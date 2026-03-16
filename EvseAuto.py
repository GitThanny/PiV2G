import time
import gc
import threading
import Adafruit_BBIO.GPIO as GPIO  # BeagleBone hardware button library
from Whitebeet import *
from CanPhoenix import *
from RelayControl import *

class Evse():
    # Define your button pins here
    START_PIN = "P8_14"
    STOP_PIN = "P8_12"

    def __init__(self, iftype, iface, mac, auto_authorize=True): 
        self.whitebeet = Whitebeet(iftype, iface, mac)
        print(f"WHITE-beet-EI firmware version: {self.whitebeet.version}")
        self.relay = RelayControl("P8_17")
        self.relay.turn_on()
        self.CanPhoenix = CanPhoenix()
        
        self.schedule = None
        self.evse_config = None
        self.auto_authorize = auto_authorize
        self.charging = False
        
        # Background update thread support
        self._poll_count = 0  
        self._update_thread = None
        self._update_running = False
        self._update_params_lock = threading.Lock()
        self._latest_charging_params = None
        self._gc_manual_collect_counter = 0

        # Embedded System Button Support
        self._force_stop_flag = False

        # ==========================================
        # HARDWARE BUTTON SETUP
        # ==========================================
        # Setup pins with internal pull-up resistors
        GPIO.setup(self.START_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self.STOP_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # STOP PIN: Triggers when pressed to GND
        GPIO.add_event_detect(self.STOP_PIN, GPIO.FALLING, callback=self._stop_button_callback, bouncetime=1000)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if hasattr(self, "whitebeet"):
            del self.whitebeet

    def __del__(self):
        if hasattr(self, "whitebeet"):
            del self.whitebeet

    # ==========================================
    # PHYSICAL BUTTON INTERRUPT HANDLING
    # ==========================================
    def _stop_button_callback(self, channel):
        """Triggered instantly when the physical STOP button is pressed."""
        print("\n[USER ABORT] Physical STOP button pressed! Canceling session and resetting...")
        self._force_stop_session()

    def _force_stop_session(self):
        """Safely isolate hardware and flag the loop to break back to idle state."""
        self._force_stop_flag = True
        self.charging = False
        
        # Safely attempt to stop the CAN loop if it was started
        try:
            self.CanPhoenix.StopCanLoop()
        except AttributeError:
            pass

        self.CanPhoenix.stop()
        self.relay.turn_off()
        
        try:
            # Tell Whitebeet to stop whatever it's doing
            self.whitebeet.v2gEvseStopCharging()
            self.whitebeet.v2gEvseStopListen()
        except Exception as e:
            pass
            
        print("[USER ABORT] Hardware isolated. Returning to Idle State...\n")
    # ==========================================

    def _initialize(self):
        """
        Turns on the CP and SLAC services so the car can be detected.
        """
        print("Set the CP mode to EVSE")
        self.whitebeet.controlPilotSetMode(1)
        print("Set the CP duty cycle to 100%")
        self.whitebeet.controlPilotSetDutyCycle(100)
        print("Start the CP service")
        self.whitebeet.controlPilotStart()
        print("Start SLAC in EVSE mode")
        self.whitebeet.slacStart(1)
        time.sleep(2)

    def _waitEvConnected(self, timeout):
        """
        We check for the state on the CP. When there is no EV connected we have state A on CP.
        When an EV connects the state changes to state B and we can continue with further steps.
        """
        timestamp_start = time.time()
        cp_state = self.whitebeet.controlPilotGetState()
        if cp_state == 1:
            print("EV already connected")
            return True
        elif cp_state > 1:
            print("CP in wrong state: {}".format(cp_state))
            return False
        else:
            print("Wait until an EV connects")
            while True:
                # Break out early if user pressed STOP button while we were waiting
                if self._force_stop_flag:
                    return False

                cp_state = self.whitebeet.controlPilotGetState()
                if timeout != None and timestamp_start + timeout > time.time():
                    return False
                if cp_state == 0:
                    time.sleep(0.1)
                elif cp_state == 1:
                    print("EV connected")
                    return True
                else:
                    print("CP in wrong state: {}".format(cp_state))
                    return False

    def _handleEvConnected(self):
        """
        When an EV connects, start the CAN loop and initiate SLAC matching.
        """
        print("[CAN] Starting CAN bus loop...")
        self.CanPhoenix.StartCanLoop()

        print("Start SLAC matching")
        self.whitebeet.slacStartMatching()
        print("Set duty cycle to 5%")
        self.whitebeet.controlPilotSetDutyCycle(5)
        try:
            if self.whitebeet.slacMatched() == True:
                
                # Check if STOP button was pressed during SLAC match
                if self._force_stop_flag:
                    print("[ABORT] SLAC matched, but STOP button was pressed. Aborting...")
                    return False
                    
                print("SLAC matching successful")
                self._handleNetworkEstablished()
                return True
            else:
                print("SLAC matching failed")
                return False
        except TimeoutError as e:
            print(e)
            return False

    def _handleNetworkEstablished(self):
        """
        When SLAC was successful we can start the V2G module.
        """
        print("Set V2G mode to EVSE")
        self.whitebeet.v2gSetMode(1)

        self.evse_config = {
            "evse_id_DIN": '+49*123*456*789',
            "evse_id_ISO": 'DE*A23*E45B*78C',
            "protocol": [0, 1], 
            "payment_method": [0],
            "energy_transfer_mode": [0, 1, 2, 3, 4, 5],
            "certificate_installation_support": False,
            "certificate_update_support": False,
        }
        self.whitebeet.v2gEvseSetConfiguration(self.evse_config)

        self.dc_charging_parameters = {
            'isolation_level': 1,
            'min_voltage': self.CanPhoenix.getEvseMinVoltage(),
            'min_current': self.CanPhoenix.getEvseMinCurrent(),
            'max_voltage': self.CanPhoenix.getEvseMaxVoltage(),
            'max_current': self.CanPhoenix.getEvseMaxCurrent(),
            'max_power': self.CanPhoenix.getEvseMaxPower(),
            'peak_current_ripple': int(self.CanPhoenix.getEvseDeltaCurrent()),
            'status': 0
        }
        self.whitebeet.v2gEvseSetDcChargingParameters(self.dc_charging_parameters)

        self.ac_charging_parameters = {
            'rcd_status': 0,
            'nominal_voltage': self.CanPhoenix.getEvseMaxVoltage(),
            'max_current': self.CanPhoenix.getEvseMaxCurrent(),
        }
        self.whitebeet.v2gEvseSetAcChargingParameters(self.ac_charging_parameters)

        time.sleep(0.1)
        print("Start V2G")
        self.whitebeet.v2gEvseStartListen()
        
        while not self._force_stop_flag:
            if self.charging:
                
                if gc.isenabled():
                    gc.disable()
                    print("[GC] Automatic garbage collection disabled during charging")

                loop_start = time.time()
                recv_start = time.time()
                
                id, data = self.whitebeet.v2gEvseReceiveRequestSilent()
                
                recv_time = (time.time() - recv_start) * 1000

                charging_parameters = {
                    'isolation_level': 1,
                    'present_voltage': int(self.CanPhoenix.getEvsePresentVoltage()),
                    'present_current': int(self.CanPhoenix.getEvsePresentCurrent()),
                    'max_voltage': int(self.CanPhoenix.getEvseMaxVoltage()),
                    'max_current': int(self.CanPhoenix.getEvseMaxCurrent()),
                    'max_power': int(self.CanPhoenix.getEvseMaxPower()),
                    'status': 0,
                }

                update_start = time.time()
                update_ok = self.whitebeet.v2gEvseUpdateDcChargingParametersFast(charging_parameters)
                if not update_ok:
                    print("[UPDATE] Fast update failed, retrying with normal method")
                    try:
                        self.whitebeet.v2gEvseUpdateDcChargingParameters(charging_parameters)
                    except:
                        pass
                update_time = (time.time() - update_start) * 1000

                total_time = (time.time() - loop_start) * 1000
                self._poll_count += 1
                
                if total_time > 400:
                    print(f"[TIMING WARNING] Loop took {total_time:.0f}ms (should be < 500ms)")

            else:
                id, data = self.whitebeet.v2gEvseReceiveRequest()

            if id == None or data == None:
                pass
            elif id == 0x80:
                self._handleSessionStarted(data)
            elif id == 0x81:
                self._handlePaymentSelected(data)
            elif id == 0x82:
                self._handleRequestAuthorization(data)
            elif id == 0x83:
                self._handleEnergyTransferModeSelected(data)
            elif id == 0x84:
                self._handleRequestSchedules(data)
            elif id == 0x85:
                self._handleDCChargeParametersChanged(data)
            elif id == 0x86:
                self._handleACChargeParametersChanged(data)
            elif id == 0x87:
                self._handleRequestCableCheck(data)
            elif id == 0x88:
                self._handlePreChargeStarted(data)
            elif id == 0x89:
                self._handleRequestStartCharging(data)
            elif id == 0x8A:
                self._handleRequestStopCharging(data)
            elif id == 0x8B:
                self._handleWeldingDetectionStarted(data)
            elif id == 0x8C:
                self._handleSessionStopped(data)
                break
            elif id == 0x8D:
                pass
            elif id == 0x8E:
                self._handleSessionError(data)
            elif id == 0x8F:
                self._handleCertificateInstallationRequested(data)
            elif id == 0x90:
                self._handleCertificateUpdateRequested(data)
            elif id == 0x91:
                self._handleMeteringReceiptStatus(data)
            else:
                print("Message ID not supported: {:02x}".format(id))
                break
        self.whitebeet.v2gEvseStopListen()

    def _handleSessionStarted(self, data):
        print("\"Session started\" received")
        message = self.whitebeet.v2gEvseParseSessionStarted(data)
        print("Protocol: {}".format(message['protocol']))
        print("Session ID: {}".format(message['session_id'].hex()))
        print("EVCC ID: {}".format(message['evcc_id'].hex()))
        time.sleep(2)

    def _handlePaymentSelected(self, data):
        print("\"Payment selcted\" received")
        message = self.whitebeet.v2gEvseParsePaymentSelected(data)
        print("Selected payment method: {}".format(message['selected_payment_method']))
        if message['selected_payment_method'] == 1:
            print("Contract certificate: {}".format(message['contract_certificate'].hex()))
            print("mo_sub_ca1: {}".format(message['mo_sub_ca1'].hex()))
            print("mo_sub_ca2: {}".format(message['mo_sub_ca2'].hex()))
            print("EMAID: {}".format(message['emaid'].hex()))
        time.sleep(2)

    def _handleRequestAuthorization(self, data):
        """
        Handle the RequestAuthorization notification.
        HEADLESS FIX: Automatically authorizes without requiring keyboard input.
        """
        print("\"Request Authorization\" received")
        message = self.whitebeet.v2gEvseParseAuthorizationStatusRequested(data)
        print(message['timeout'])

        print("[AUTO-AUTHORIZE] Headless mode: Vehicle automatically authorized.")
        try:
            self.whitebeet.v2gEvseSetAuthorizationStatus(True)
        except (Warning, ConnectionError) as e:
            print(f"{type(e).__name__}: {e}")
            
        time.sleep(2)

    def _handleEnergyTransferModeSelected(self, data):
        print("\"Energy transfer mode selected\" received")
        message = self.whitebeet.v2gEvseParseEnergyTransferModeSelected(data)

        if 'departure_time' in message:        
            print('Departure time: {}'.format(message['departure_time']))
        if 'energy_request' in message:
            print('Energy request: {}'.format(message['energy_request']))

        print('Maximum voltage: {}'.format(message['max_voltage']))
        self.CanPhoenix.setEvMaxVoltage(message['max_voltage'])

        if 'min_current' in message:
            print('Minimum current: {}'.format(message['min_current']))
            self.CanPhoenix.setEvMinCurrent(message['min_current'])

        print('Maximum current: {}'.format(message['max_current']))
        self.CanPhoenix.setEvMaxCurrent(message['max_current'])

        if 'max_power' in message:
            print('Maximum power: {}'.format(message['max_power']))
            self.CanPhoenix.setEvMaxPower(message['max_power'])

        if 'energy_capacity' in message:
            print('Energy Capacity: {}'.format(message['energy_capacity']))
        if 'full_soc' in message:
            print('Full SoC: {}'.format(message['full_soc']))
        if 'bulk_soc' in message:
            print('Bulk SoC: {}'.format(message['bulk_soc']))
        if 'ready' in message:
            print('Ready: {}'.format('yes' if message['ready'] else 'no'))
        if 'error_code' in message:
            print('Error code: {}'.format(message['error_code']))
        if 'soc' in message:
            print('SoC: {}'.format(message['soc']))

        if 'selected_energy_transfer_mode' in message:
            print('Selected energy transfer mode: {}'.format(message['selected_energy_transfer_mode']))
            if not message['selected_energy_transfer_mode'] in self.evse_config['energy_transfer_mode']:
                print('Energy transfer mode mismatch!')
                try:
                    self.whitebeet.v2gEvseStopCharging()
                except Warning as e:
                    print("Warning: {}".format(e))
                except ConnectionError as e:
                    print("ConnectionError: {}".format(e))
                    
        time.sleep(0.2)

    def _handleRequestSchedules(self, data):
        print("\"Request Schedules\" received")
        message = self.whitebeet.v2gEvseParseSchedulesRequested(data)
        print("Max entries: {}".format(message['max_entries']))
        maxEntry = max([len(self.schedule), message['max_entries']])
        print("Set the schedule: {}".format(self.schedule))
        try:
            self.whitebeet.v2gEvseSetSchedules(self.schedule)
        except Warning as e:
            print("Warning: {}".format(e))
        except ConnectionError as e:
            print("ConnectionError: {}".format(e))
            
        time.sleep(0.2)
        
    def _handleDCChargeParametersChanged(self, data):
        print("\"DC Charge Parameters Changed\" received")
        message = self.whitebeet.v2gEvseParseDCChargeParametersChanged(data)

        print("EV maximum current: {}A".format(message['max_current']))
        self.CanPhoenix.setEvMaxCurrent(message['max_current'])

        print("EV maximum voltage: {}V".format(message['max_voltage']))
        self.CanPhoenix.setEvMaxVoltage(message['max_voltage'])

        if 'max_power' in message:
            print("EV maximum power: {}W".format(message['max_power']))
            self.CanPhoenix.setEvMaxPower(message['max_power'])

        print('EV ready: {}'.format(message['ready']))
        print('Error code: {}'.format(message['error_code']))
        print("SOC: {}%".format(message['soc']))

        if 'target_voltage' in message:
            print("EV target voltage: {}V".format(message['target_voltage']))
            self.CanPhoenix.setEvTargetVoltage(message['target_voltage'])

        if 'target_current' in message:
            print("EV target current: {}A".format(message['target_current']))
            self.CanPhoenix.setEvTargetCurrent(message['target_current'])
        
        if 'charging_complete' in message:
            print("Charging complete: {}".format(message['charging_complete']))
        if 'bulk_charging_complete' in message:
            print("Bulk charging complete: {}".format(message['bulk_charging_complete']))
        if 'remaining_time_to_full_soc' in message:
            print("Remaining time to full SOC: {}s".format(message['remaining_time_to_full_soc']))
        if 'remaining_time_to_bulk_soc' in message:
            print("Remaining time to bulk SOC: {}s".format(message['remaining_time_to_bulk_soc']))
    
        charging_parameters = {
            'isolation_level': 1,
            'present_voltage': int(self.CanPhoenix.getEvsePresentVoltage()),
            'present_current': int(self.CanPhoenix.getEvsePresentCurrent()),
            'max_voltage': int(self.CanPhoenix.getEvseMaxVoltage()),
            'max_current': int(self.CanPhoenix.getEvseMaxCurrent()),
            'max_power': int(self.CanPhoenix.getEvseMaxPower()),
            'status': 0,
        }

        try:
            self.whitebeet.v2gEvseUpdateDcChargingParameters(charging_parameters)
        except Warning as e:
            print("Warning: {}".format(e))
        except ConnectionError as e:
            print("ConnectionError: {}".format(e))

    def _handleACChargeParametersChanged(self, data):
        print("\"AC Charge Parameters Changed\" received")
        message = self.whitebeet.v2gEvseParseACChargeParametersChanged(data)

        print("EV maximum voltage: {}V".format(message['max_voltage']))
        self.CanPhoenix.setEvMaxVoltage(message['max_voltage'])

        print("EV minimum current: {}W".format(message['min_current']))
        self.CanPhoenix.setEvMinCurrent(message['min_current'])

        print("EV maximum current: {}A".format(message['max_current']))
        self.CanPhoenix.setEvMaxCurrent(message['max_current'])

        print("Energy amount: {}A".format(message['energy_amount']))    

        charging_parameters = {
            'rcd_status': 0,
            'max_current': int(self.CanPhoenix.getEvseMaxCurrent()),
        }

        try:
            self.whitebeet.v2gEvseUpdateAcChargingParameters(charging_parameters)
        except Warning as e:
            print("Warning: {}".format(e))
        except ConnectionError as e:
            print("ConnectionError: {}".format(e))

    def _handleRequestCableCheck(self, data):
        self.charging = True
        self.relay.turn_on()
        print("\"Request Cable Check Status\" received")
        self.whitebeet.v2gEvseParseCableCheckRequested(data)
        try:
            self.whitebeet.v2gEvseSetCableCheckFinished(True)
        except Warning as e:
            print("Warning: {}".format(e))
        except ConnectionError as e:
            print("ConnectionError: {}".format(e))

    def _handlePreChargeStarted(self, data):
        print("\"Pre Charge Started\" received")
        self.whitebeet.v2gEvseParsePreChargeStarted(data)

    def _handleRequestStartCharging(self, data):
        print("\"Start Charging Requested\" received")
        message = self.whitebeet.v2gEvseParseStartChargingRequested(data)
        print("Schedule tuple ID: {}".format(message['schedule_tuple_id']))
        print("Charging profiles: {}".format(message['charging_profiles']))
        
        self.charging = True
        self._poll_count = 0  
        
        try:
            self.whitebeet.v2gEvseStartCharging()
        except Warning as e:
            print("Warning: {}".format(e))
        except ConnectionError as e:
            print("ConnectionError: {}".format(e))

    def _handleRequestStopCharging(self, data):
        self.relay.turn_off()
        print("\"Request Stop Charging\" received")
        message = self.whitebeet.v2gEvseParseStopChargingRequested(data)
        print('Timeout: {}'.format(message['timeout']))
        print('Timeout: {}'.format('yes' if message['renegotiation'] else 'no'))
        
        try:
            self.CanPhoenix.StopCanLoop()
        except AttributeError:
            pass
        self.CanPhoenix.stop()
        
        try:
            self.whitebeet.v2gEvseStopCharging()
        except Warning as e:
            print("Warning: {}".format(e))
        except ConnectionError as e:
            print("ConnectionError: {}".format(e))

    def _handleWeldingDetectionStarted(self, data):
        print("\"Welding Detection Started\" received")
        self.whitebeet.v2gEvseParseWeldingDetectionStarted(data)

    def _handleSessionStopped(self, data):
        self.relay.turn_off()
        self.charging = False
        print("\"Session stopped\" received")
        message = self.whitebeet.v2gEvseParseSessionStopped(data)
        print('Closure type: {}'.format(message['closure_type']))
        
        try:
            self.CanPhoenix.StopCanLoop()
        except AttributeError:
            pass
        self.CanPhoenix.stop()

    def _handleSessionError(self, data):
        self.relay.turn_off()
        print("\"Session Error\" received")
        self.charging = False
        message = self.whitebeet.v2gEvseParseSessionError(data)
        
        try:
            self.CanPhoenix.StopCanLoop()
        except AttributeError:
            pass
        self.CanPhoenix.stop()

        error_messages = {
            0: 'Unspecified',
            1: 'Sequence error',
            2: 'Service ID invalid',
            3: 'Unknown session',
            23: 'Charge parameter timeout reached'
        }

        print('Session error: {}: {}'.format(message['error_code'], error_messages.get(message['error_code'], 'Unknown')))
        try:
            self.whitebeet.v2gEvseStopCharging()
        except Warning as e:
            print("Warning: {}".format(e))
            self.whitebeet.v2gEvseStopListen()
        except ConnectionError as e:
            print("ConnectionError: {}".format(e))
            self.whitebeet.v2gEvseStopListen()

    def _handleCertificateInstallationRequested(self, data):
        print("\"Certificate Installation Requested\" received")
        message = self.whitebeet.v2gEvseParseCertificateInstallationRequested(data)
        print('Timeout: {}'.format(message['timeout']))
        print('EXI request: {}'.format(message['exi_request']))
        status = 2
        certificationResponse = []

    def _handleCertificateUpdateRequested(self, data):
        print("\"Certificate Update Requested\" received")
        message = self.whitebeet.v2gEvseParseCertificateUpdateRequested(data)
        print('Timeout: {}'.format(message['timeout']))
        print('EXI request: {}'.format(message['exi_request']))
        status = 2
        certificationResponse = []
            
    def _handleMeteringReceiptStatus(self, data):
        print("\"Metering Receipt Status\" received")
        message = self.whitebeet.v2gEvseParseMeteringReceiptStatus(data)
        print('Metering receipt status: {}'.format('verified' if message['status'] == True else 'not verified'))

    def getCanPhoenix(self):
        if hasattr(self, "CanPhoenix"):
            return self.CanPhoenix
        else:
            return None

    def getWhitebeet(self):
        if hasattr(self, "whitebeet"):
            return self.whitebeet
        else:
            return None

    def setSchedule(self, schedule):
        if isinstance(schedule, dict) == False:
            print("Schedule needs to be of type dict")
            return False
        else:
            self.schedule = schedule
            return True

    def loop(self):
        """
        This will handle continuous charging sessions of the EVSE.
        The user must press START to activate the station BEFORE plugging in the EV.
        """
        
        while True:
            # Clear flag if we just aborted
            if getattr(self, '_force_stop_flag', False):
                time.sleep(2) 
                self._force_stop_flag = False  

            # Re-enable GC between sessions to clean up memory safely
            if not gc.isenabled():
                gc.enable()
                gc.collect()

            # SILENCE EVERYTHING WHILE IDLE
            print("\n[SYSTEM] Silencing Whitebeet and clearing old sessions...")
            try:
                self.whitebeet.v2gEvseStopListen()
                self.whitebeet.slacStop()
                self.whitebeet.controlPilotStop()
            except Exception:
                pass
                
            print("\n=======================================================")
            print(">>> EVSE IDLE: Waiting for user to activate station <<<")
            print("=======================================================\n")
            
            # 1. WAIT FOR START BUTTON FIRST
            print("[UI] Please press START button (P8_14) to turn on the charger...")
            
            # Wait while the circuit is LOW (Active-High wiring)
            while GPIO.input(self.START_PIN) == GPIO.LOW and not self._force_stop_flag:
                time.sleep(0.1) 
            
            if self._force_stop_flag:
                self._force_stop_flag = False
                continue

            print("\n[UI] START button pressed! Activating CP and SLAC services...")
            
            # 2. START THE HARDWARE READINESS
            self._initialize()
            
            print("\n[UI] Charger Active! >>> Please plug in the EV now... <<<")
            
            # 3. NOW WAIT FOR THE EV TO CONNECT
            if self._waitEvConnected(None):
                
                # Proceed instantly to SLAC matching as soon as the cable connects
                if not self._force_stop_flag:
                    print("\n[UI] EV Connected! Initiating V2G SLAC matching instantly...")
                    self._handleEvConnected()
            
            # If we get here, the session ended naturally or via the Stop button
            print("[EVSE] Session loop exited. Resetting hardware for next EV...")
            
            try:
                self.CanPhoenix.StopCanLoop()
            except AttributeError:
                pass

            self.CanPhoenix.stop()
            self.relay.turn_off()
            time.sleep(2)
