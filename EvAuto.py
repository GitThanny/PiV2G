import time
import traceback
import sys
from Logger import *
from scapy.automaton import Message
from Whitebeet import *
from Battery import *

class Ev():

    def __init__(self, iftype, iface, mac):
        self.logger = Logger()
        self.whitebeet = Whitebeet(iftype, iface, mac)
        print(f"WHITE-beet-PI firmware version: {self.whitebeet.version}")

        self.battery = Battery()

        self.scheduleStartTime = time.time()

        self.config = {}
        self.config["evid"] = bytes.fromhex(mac.replace(":",""))
        self.config["protocol_count"] = 2
        self.config["protocols"] = [0, 1]
        self.config["payment_method_count"] = 1
        self.config["payment_method"] = [0]
        self.config["energy_transfer_mode_count"] = 2
        self.config["energy_transfer_mode"] = [0, 4]
        self.config["battery_capacity"] = self.battery.getCapacity()

        self.DCchargingParams = {}
        self.ACchargingParams = {}

        self._updateChargingParameter()

        self.schedule = None
        self.currentSchedule = 0
        self.currentEnergyTransferMode = -1
        self.currentAcMaxCurrent = 0
        self.currentAcNominalVoltage = 0

        self.state = "init"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if hasattr(self, "whitebeet"):
            del self.whitebeet

    def __del__(self):
        if hasattr(self, "whitebeet"):
            del self.whitebeet

    def load(self, configDict):
        if "battery" in configDict:
            for key in configDict["battery"]:
                try:
                    if key == "capacity":
                        self.battery.setCapacity(configDict["battery"][key])
                    elif key == "level":
                        self.battery.setLevel(configDict["battery"][key])
                    else:
                        setattr(self.battery, key, configDict["battery"][key])
                except:
                    print(key + " not in ev.battery")
                    continue

        if "ev" in configDict:
            for key in configDict["ev"]:
                try:
                    if key == "evid":
                        self.config[key] = bytes.fromhex(configDict["ev"][key].replace(":",""))
                    else:
                        self.config[key] = configDict["ev"][key]
                except:
                    print(key + " not in EV.config")
                    continue

        self._updateChargingParameter()

    def log_backtrace(self):
        backtrace_frames = traceback.extract_stack()
        filename, line_number, function, code = backtrace_frames[0]
        msg = "Backtrace: %s:%s: %s()\n" % (filename, line_number, function)
        detail = ""
        for frame in backtrace_frames[:-1]:
            filename, line_number, function, code = frame
            detail += "%s:%s: %s()\n" % (filename, line_number, function)
        print(msg, detail)

    def _initialize(self):
        """
        Initializes hardware. Added try/except to ignore 'Code 5' if 
        the systemd service restarts while the module is still active.
        """
        print("Set the CP mode to EV")
        self.whitebeet.controlPilotSetMode(0)
        
        print("Start the CP service")
        try:
            self.whitebeet.controlPilotStart()
        except Warning as e:
            if "return code: 5" not in str(e): raise e
            print("\tCP service already running.")

        print("Set the CP state to State B")
        self.whitebeet.controlPilotSetResistorValue(0)
        
        print("Set SLAC to EV mode")
        self.whitebeet.slacSetValidationConfiguration(0)
        
        print("Start SLAC")
        try:
            self.whitebeet.slacStart(0)
        except Warning as e:
            if "return code: 5" not in str(e): raise e
            print("\tSLAC already running.")
            
        time.sleep(2)
    
    def _updateChargingParameter(self):
        if any((True for x in [0, 1, 2, 3] if x in self.config['energy_transfer_mode'])):
            self.DCchargingParams["min_voltage"] = 0
            self.DCchargingParams["min_current"] = 0
            self.DCchargingParams["min_power"] = self.DCchargingParams["min_voltage"] * self.DCchargingParams["min_current"]
            self.DCchargingParams["status"] = 0
            self.DCchargingParams["energy_request"] = self.battery.getCapacity() * (100 - self.battery.getSOC()) // 100
            self.DCchargingParams["departure_time"] = 100000000
            self.DCchargingParams["max_voltage"] = self.battery.max_voltage
            self.DCchargingParams["max_current"] = self.battery.max_current
            self.DCchargingParams["max_power"] = self.battery.max_power
            self.DCchargingParams["soc"] = self.battery.getSOC()
            self.DCchargingParams["target_voltage"] = self.battery.target_voltage
            self.DCchargingParams["target_current"] = self.battery.target_current
            self.DCchargingParams["full_soc"] = self.battery.full_soc
            self.DCchargingParams["bulk_soc"] = self.battery.bulk_soc

        if any((True for x in [4, 5] if x in self.config['energy_transfer_mode'])):
            self.ACchargingParams["min_voltage"] = 220
            self.ACchargingParams["min_current"] = 1
            self.ACchargingParams["min_power"] = self.ACchargingParams["min_voltage"] * self.ACchargingParams["min_current"]
            self.ACchargingParams["max_current"] = self.battery.max_current
            self.ACchargingParams["max_power"] = self.battery.max_power
            self.ACchargingParams["max_voltage"] = self.battery.max_voltage
            self.ACchargingParams["energy_request"] = self.battery.getCapacity() * (100 - self.battery.getSOC()) // 100
            self.ACchargingParams["departure_time"] = 1000000

    def _waitEvseConnected(self, timeout):
        timestamp_start = time.time()
        cp_dc = self.whitebeet.controlPilotGetDutyCycle()
        if cp_dc < 10.0 and cp_dc > 0.1:
            print("EVSE connected")
            return True
        else:
            print("Wait until an EVSE connects")
            while True:
                cp_dc = self.whitebeet.controlPilotGetDutyCycle()
                if timeout != None and timestamp_start + timeout < time.time():
                    return False
                if cp_dc < 10.0 and cp_dc > 0.1:
                    print("EVSE connected")
                    return True
                else:
                    time.sleep(0.1)

    def _handleEvseConnected(self):
        print("Start SLAC matching")
        time.sleep(5.0)
        self.whitebeet.slacStartMatching()
        try:
            if self.whitebeet.slacMatched() == True:
                print("SLAC matching successful")
                return self._handleNetworkEstablished()
            else:
                print("SLAC matching failed")
                return False
        except TimeoutError as e:
            print(e)
            return False

    def _handleNetworkEstablished(self):
        print("Set V2G mode to EV")
        self.whitebeet.v2gSetMode(0)
        print("Set V2G configuration")
        self.whitebeet.v2gEvSetConfiguration(self.config)

        if any((True for x in [0, 1, 2, 3] if x in self.config['energy_transfer_mode'])):
            print("Set DC charging parameters")
            self.DCchargingParams["soc"] = self.battery.getSOC()
            self.whitebeet.v2gSetDCChargingParameters(self.DCchargingParams)

        if any((True for x in [4, 5] if x in self.config['energy_transfer_mode'])):
            print("Set AC charging parameters")      
            self.whitebeet.v2gSetACChargingParameters(self.ACchargingParams)

        print("Create new charging session")
        self.whitebeet.v2gStartSession()
        self.state = "sessionStarting"

        # 10 Second Timeout Watchdog
        v2g_start_time = time.time()

        oldVal = self.whitebeet.controlPilotGetDutyCycle()
        print("ControlPilot duty cycle: " + str(oldVal))
        
        while not (self.state == "end"):
            
            # Check for 10s hang
            if time.time() - v2g_start_time > 10:
                print("!!! V2G Handshake Timeout: Stalled for > 10s !!!")
                raise TimeoutError("V2G stalled")

            # EV state machine
            if self.state == "sessionStarting":
                pass

            elif self.state == "sessionStared":
                pass

            elif self.state == "cableCheckReady":
                try:
                    print("Change State to State C")
                    self.whitebeet.controlPilotSetResistorValue(1)
                    self.whitebeet.v2gStartCableCheck()
                    self.state = "cableCheckStarted"
                except (Warning, ConnectionError) as e:
                    print("Error: {}".format(e))
                    raise e

            elif self.state == "cableCheckStarted":
                pass

            elif self.state == "cableCheckFinished":
                pass

            elif self.state == "preChargingReady":
                try:
                    self.state = "preChargingStarted"
                    self.whitebeet.v2gStartPreCharging()
                except (Warning, ConnectionError) as e:
                    print("Error: {}".format(e))
                    raise e

            elif self.state == "preChargingStarted":
                pass

            elif self.state == "chargingReady" and self.schedule is not None:
                startCharging = True
                self.battery.setEnergyTransferMode(self.currentEnergyTransferMode)
                print(str(self.battery))

                if self.currentEnergyTransferMode in [0,1,2,3]:
                    if self.battery.in_voltage > self.battery.max_voltage:
                        startCharging = False
                    if (self.battery.in_voltage > self.battery.target_voltage + self.battery.target_voltage_delta) \
                    or (self.battery.in_voltage < self.battery.target_voltage - self.battery.target_voltage_delta):
                        startCharging = False
                    if self.battery.in_current > self.battery.max_current:
                        startCharging = False
                
                elif self.currentEnergyTransferMode in [4,5]:
                    if self.battery.in_voltage > self.battery.max_voltage_AC:
                        startCharging = False
                    if (self.battery.in_current > self.battery.max_current_AC) \
                    or (self.battery.in_current < self.battery.min_current_AC):
                        startCharging = False

                if self.battery.max_power < self.battery.in_voltage * self.battery.in_current:
                    startCharging = False

                if startCharging == True and self.battery.is_charging == False:
                    self.state = "waitChargingStarted"
                    try:
                        self.battery.is_charging = True
                        self.whitebeet.v2gStartCharging()
                        print("Change State to State C")
                        self.whitebeet.controlPilotSetResistorValue(1)
                    except (Warning, ConnectionError) as e:
                        print("Error: {}".format(e))
                        raise e

                if startCharging == False and self.battery.is_charging == True:
                    try:                        
                        self.whitebeet.v2gStopCharging(False)
                    except (Warning, ConnectionError) as e:
                        print("Error: {}".format(e))
                        raise e

            elif self.state == "chargingStarted" and self.schedule is not None:
                if self.battery.tickSimulation():
                    if ({'start', 'power', 'interval'} <= set(self.schedule)):
                        if time.time() >= (self.scheduleStartTime + self.schedule['start'][self.currentSchedule]):
                            if(len(self.schedule['power']) > self.currentSchedule):
                                self.currentSchedule += 1
                            else:
                                self.whitebeet.v2gStopCharging(False)
                                print("Last profile entry finished")
                                
                        maxPower = min(self.schedule['power'][self.currentSchedule], self.battery.max_power)
                        self.battery.target_current = maxPower // self.battery.target_voltage

                    if self.battery.getSOC() < self.battery.full_soc:
                        self.DCchargingParams["soc"] = self.battery.getSOC()
                        try:                  
                            if self.currentEnergyTransferMode in [0,1,2,3]:
                                self.whitebeet.v2gUpdateDCChargingParameters(self.DCchargingParams)
                            elif self.currentEnergyTransferMode in [4,5]:
                                self.whitebeet.v2gUpdateACChargingParameters(self.ACchargingParams)
                        except (Warning, ConnectionError) as e:
                            print("Error: {}".format(e))
                            raise e

                    elif self.battery.getSOC() >= self.battery.full_soc and self.battery.is_charging == True:
                        self.battery.is_charging = False
                        print("charging done")
                        try:                        
                            self.whitebeet.v2gStopCharging(False)
                        except (Warning, ConnectionError) as e:
                            print("Error: {}".format(e))
                            raise e

            elif self.state == "chargingStopped":
                self.battery.is_charging = False
                try:
                    self.state = "waitSessionStopped"
                    self.whitebeet.v2gStopSession()
                except (Warning, ConnectionError) as e:
                    print("Error: {}".format(e))
                    raise e

            elif self.state == "postChargingReady":
                pass

            elif self.state == "sessionStopped":
                self.battery.is_charging = False
                self.state = "end"

            # receive messages from whitebeet
            try:
                id, data = self.whitebeet.v2gEvReceiveRequest()
                if id == 0xC3: self._handleScheduleReceived(data)
                elif id == 0xC0: self._handleSessionStarted(data)
                elif id == 0xC1: self._handleDCChargeParametersChanged(data)
                elif id == 0xC2: self._handleACChargeParametersChanged(data)
                elif id == 0xC4: self._handleCableCheckReady(data)
                elif id == 0xC5: self._handleCableCheckFinished(data)
                elif id == 0xC6: self._handlePreChargingReady(data)
                elif id == 0xC7: self._handleChargingReady(data)
                elif id == 0xC8: self._handleChargingStarted(data)
                elif id == 0xC9: self._handleChargingStopped(data)
                elif id == 0xCA: self._handlePostChargingReady(data)
                elif id == 0xCB: self._handleSessionStopped(data)
                elif id == 0xCC: self._handleNotificationReceived(data)
                elif id == 0xCD: self._handleSessionError(data)
                else:
                    print("Message ID not supported: {:02x}".format(id))
                    break
            except TimeoutError:
                pass
            except Exception as e:
                print(f"Handshake Error: {e}")
                raise e
            
        self.whitebeet.controlPilotSetResistorValue(0)
        try:
            self.whitebeet.v2gStopSession()
        except:
            pass
        return True

    # (Methods _handleSessionStarted through _handleSessionError remain the same)
    def _handleSessionStarted(self, data):
        print("\"Session started\" received")
        message = self.whitebeet.v2gEvParseSessionStarted(data)
        self.state = "sessionStared"
        print("\tProtocol: {}".format(message['protocol']))
        print("\tSession ID: {}".format(message['session_id'].hex()))
        print("\tEVSE ID: {}".format(message['evse_id'].hex()))
        print("\tPayment method: {}".format(message['payment_method']))
        print("\tEnergy transfer mode: {}".format(message['energy_transfer_mode']))
        if message['energy_transfer_mode'] in self.config['energy_transfer_mode']:
            self.currentEnergyTransferMode = message["energy_transfer_mode"]
        else:
            print("\t\twrong energy transfer mode!")

    def _handleDCChargeParametersChanged(self, data):
        print("\"DC Charge Parameters Changed\" received")
        message = self.whitebeet.v2gEvParseDCChargeParametersChanged(data)
        if message["evse_status"] != 0: self.battery.is_charging = False
        self.battery.in_voltage = message["evse_present_voltage"]
        self.battery.in_current = message["evse_present_current"]

    def _handleACChargeParametersChanged(self, data):
        print("\"AC Charge Parameter changed\" received")
        message = self.whitebeet.v2gEvParseACChargeParametersChanged(data)
        self.battery.in_voltage = message['nominal_voltage']
        self.currentAcMaxCurrent = message["max_current"]
        self.battery.in_current = self.schedule['power'][self.currentSchedule] / self.battery.in_voltage
        if message["rcd"] == True:
            self.whitebeet.v2gStopCharging(False)

    def _handleScheduleReceived(self, data):
        print("\"Schedule Received\" received")
        message = self.whitebeet.v2gEvParseScheduleReceived(data)
        start = [e['start'] for e in message['entries']]
        interval = [e['interval'] for e in message['entries']]
        power = [e['power'] for e in message['entries']]
        self.schedule = {"schedule_tuple_id": 1, "charging_profile_entries_count": message['entries_count'],
                         "start": start, "interval": interval, "power": power}
        self.scheduleStartTime = time.time()
        self.currentSchedule = 0
        try:
            self.whitebeet.v2gSetChargingProfile(self.schedule)
        except:
            raise

    def _handleCableCheckReady(self, data):
        self.whitebeet.v2gEvParseCableCheckReady(data)
        self.state = "cableCheckReady"

    def _handleCableCheckFinished(self, data):
        self.whitebeet.v2gEvParseCableCheckFinished(data)
        self.state = "cableCheckFinished"

    def _handlePreChargingReady(self, data):
        self.whitebeet.v2gEvParsePreChargingReady(data)
        self.state = "preChargingReady"

    def _handleChargingReady(self, data):
        self.whitebeet.v2gEvParseChargingReady(data)
        if not self.state == "chargingStarted": self.state = "chargingReady"

    def _handleChargingStarted(self, data):
        self.whitebeet.v2gEvParseChargingStarted(data)
        self.state = "chargingStarted"

    def _handleChargingStopped(self, data):
        print("\"Charging Stopped\" received")
        self.state = "chargingStopped"
        self.whitebeet.controlPilotSetResistorValue(0)

    def _handlePostChargingReady(self, data):
        self.whitebeet.v2gEvParsePostChargingReady(data)
        self.state = "postChargingReady"
        
    def _handleNotificationReceived(self, data):
        message = self.whitebeet.v2gEvParseNotificationReceived(data)
        if message["type"] == 0: self.battery.is_charging = False

    def _handleSessionStopped(self, data):
        self.whitebeet.v2gEvParseSessionStopped(data)
        self.state = "sessionStopped"
        self.whitebeet.controlPilotSetResistorValue(0)
    
    def _handleSessionError(self, data):
        print("\"Session Error\" received")
        self.battery.is_charging = False

    def getBattery(self):
        return getattr(self, "battery", None)

    def getWhitebeet(self):
        return getattr(self, "whitebeet", None)

    def loop(self):
        """
        FULL UPDATE: The loop is now autonomous and self-healing.
        It catches all errors, resets the link, and tries again.
        """
        # Run hardware setup once (with protection against 'Code 5')
        self._initialize()

        while True:
            try:
                print("\n" + "="*40)
                print("--- STARTING NEW EV CHARGING CYCLE ---")
                print("="*40)
                
                # Reset internal state machine tracking
                self.state = "init"

                # 1. Wait for physical EVSE connection
                if self._waitEvseConnected(None):
                    
                    # 2. Start SLAC and V2G (Any error inside will raise to this try block)
                    self._handleEvseConnected()

            except (ConnectionError, TimeoutError, Warning, Exception) as e:
                print(f"\n[!] RECOVERING FROM ERROR: {e}")

            # 3. CRITICAL RESET: Set resistor to State B to signal EVSE to restart
            print("Resetting Link (State B)...")
            try:
                self.whitebeet.controlPilotSetResistorValue(0)
            except:
                pass
            
            # Cooldown to prevent CPU/Hardware flooding
            print("Cooling down 5s before next attempt...")
            time.sleep(5)
