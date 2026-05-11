import multiprocessing
import time
import sys
from scapy.all import conf, Ether, sniff, get_if_hwaddr, Raw

from SUTAdapter import *
from FramingAPIDef import *

class EthernetAdapter(SUTAdapter):
    def __init__(self):
        self.recv_process = None
        self.queue_rx = multiprocessing.Manager().Queue()
        self.queue_tx = multiprocessing.Manager().Queue()

        self.sut_ip = "" # Kept for backward compatibility with FreeV2G arguments
        self.sut_interface = ""
        self.dut_mac = None
        self.packet = None
        self.socket = None
        
        # Force Scapy to use its native capture engine
        conf.use_pcap = True

    def send(self, data):
        if len(data) > 1450:
            print("Alert: Sending large frame")

        # Use Scapy's native L2 socket to send the payload
        # b"\x00\x04" is the White-beet Control Header version/type
        self.socket.send(self.packet / (b"\x00\x04" + len(data).to_bytes(2, "big") + data))

    def receive(self):
        if not self.queue_rx.empty():
            return self.queue_rx.get_nowait()
        return None

    def pkt_callback(self, packet):
        try:
            # Extract everything after the Ethernet header
            raw_data = bytes(packet[Ether].payload)
            if len(raw_data) < 5:
                return None
            
            # Strip the 4-byte Control Header to get to the Framing Protocol
            payload = raw_data[4:]

            # Check for the Start of Frame marker (0xC0)
            marker = payload[0]
            if marker != START_OF_FRAME:
                return None

            pheader = payload[1:6]
            pbytes = int.from_bytes(pheader[3:5], 'big')

            pbytedata = b""
            if pbytes > 0:
                pbytedata = payload[6:6+pbytes]

            pdata = pheader + pbytedata if pbytes > 0 else pheader
            pbytes_total = pbytes + 5

            crc = int.from_bytes(payload[pbytes_total+1:pbytes_total+2], "big")
            end_marker = payload[pbytes_total+2]

            # Check for the End of Frame marker (0xC1)
            if end_marker == END_OF_FRAME:
                frame = self.pack_and_parse_frame(
                    b"\xc0" + pdata + crc.to_bytes(1, "big") + b"\xc1"
                )
                self.queue_rx.put_nowait(frame)
            else:
                print("Could not catch end of frame")
                
        except Exception as e:
            # Prevent malformed packets from crashing the background sniffing process
            pass

    def process_receive(self):
        # Pure Python Scapy Sniffing - No pylibpcap required!
        sniff(
            filter=f"ether proto 0x6003 and ether src {self.dut_mac}",
            iface=self.sut_interface,
            prn=self.pkt_callback,
            store=0 # CRITICAL: Don't store packets in RAM, process and drop them
        )

    def start(self):
        # Handle FreeV2G sometimes passing the MAC string into the sut_ip variable
        if self.dut_mac is None and self.sut_ip and ":" in self.sut_ip:
            self.dut_mac = self.sut_ip

        if self.dut_mac is None:
            raise AssertionError("[!] Target MAC address not set! Ensure you use '-m MAC_ADDRESS'")

        # Initialize the Scapy Layer 2 Socket
        self.socket = conf.L2socket(iface=self.sut_interface)
        
        # Pre-build the Ethernet packet target (Source MAC is auto-filled by Scapy)
        self.packet = Ether(src=get_if_hwaddr(self.sut_interface), dst=self.dut_mac, type=0x6003)

        # Start the sniffing listener in a background CPU process
        self.recv_process = multiprocessing.Process(target=self.process_receive)
        self.recv_process.start()

        # Let the sniffing process initialize
        time.sleep(1)

    def stop(self):
        if self.recv_process:
            self.recv_process.terminate()
        if self.socket:
            self.socket.close()

    def holding_data(self):
        return not self.queue_rx.empty()

    def clear_queues(self):
        while not self.queue_rx.empty():
            self.queue_rx.get_nowait()
