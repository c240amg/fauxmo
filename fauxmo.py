#!/usr/bin/env python

# For a complete discussion, see http://www.makermusings.com

import email.utils
import requests
import select
import socket
import struct
import sys
import time
import urllib
import uuid

# This XML is the minimum needed to define one of our virtual switches
# to the Amazon Echo

SETUP_XML = """<?xml version="1.0"?>
<root xmlns="urn:Belkin:device-1-0">
    <device>
        <deviceType>urn:Belkin:device:controllee:1</deviceType>
        <friendlyName>%(device_name)s</friendlyName>
        <manufacturer>Belkin International Inc.</manufacturer>
        <modelName>Emulated Socket</modelName>
        <modelNumber>1.0</modelNumber>
        <UDN>uuid:Socket-1_0-%(device_serial)s</UDN>
    </device>
</root>
"""

GETBINARY_XML = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetBinaryStateResponse xmlns:u="urn:Belkin:service:basicevent:1">
      <BinaryState>%(binary_state)s</BinaryState>
    </u:GetBinaryStateResponse>
  </s:Body>
</s:Envelope>
"""

DEBUG = True

def dbg(msg):
    global DEBUG
    if DEBUG:
        print(msg)
        sys.stdout.flush()



# A simple utility class to wait for incoming data to be
# ready on a socket.

class poller:
    def __init__(self):
        if 'poll' in dir(select):
            self.use_poll = True
            self.poller = select.poll()
        else:
            self.use_poll = False
        self.targets = {}

    def add(self, target, fileno=None):
        if not fileno:
            fileno = target.fileno()
        if self.use_poll:
            self.poller.register(fileno, select.POLLIN)
        self.targets[fileno] = target

    def remove(self, target, fileno=None):
        if not fileno:
            fileno = target.fileno()
        if self.use_poll:
            self.poller.unregister(fileno)
        del (self.targets[fileno])

    def poll(self, timeout=0):
        if self.use_poll:
            ready = self.poller.poll(timeout)
        else:
            ready = []
            if len(self.targets) > 0:
                (rlist, wlist, xlist) = select.select(self.targets.keys(), [], [], timeout)
                ready = [(x, None) for x in rlist]
        for one_ready in ready:
            target = self.targets.get(one_ready[0], None)
            if target:
                target.do_read(one_ready[0])


# Base class for a generic UPnP device. This is far from complete
# but it supports either specified or automatic IP address and port
# selection.

class upnp_device(object):
    this_host_ip = None

    @staticmethod
    def local_ip_address():
        if not upnp_device.this_host_ip:
            temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                temp_socket.connect(('8.8.8.8', 53))
                upnp_device.this_host_ip = temp_socket.getsockname()[0]
            except:
                upnp_device.this_host_ip = '127.0.0.1'
            del (temp_socket)
            dbg("got local address of %s" % upnp_device.this_host_ip)
        return upnp_device.this_host_ip

    def __init__(self, listener, poller, port, root_url, server_version, persistent_uuid, other_headers=None,
                 ip_address=None):
        self.listener = listener
        self.poller = poller
        self.port = port
        self.root_url = root_url
        self.server_version = server_version
        self.persistent_uuid = persistent_uuid
        self.uuid = uuid.uuid4()
        self.other_headers = other_headers

        if ip_address:
            self.ip_address = ip_address
        else:
            self.ip_address = upnp_device.local_ip_address()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind((self.ip_address, self.port))
        self.socket.listen(5)
        if self.port == 0:
            self.port = self.socket.getsockname()[1]
        self.poller.add(self)
        self.client_sockets = {}
        self.listener.add_device(self)

    def fileno(self):
        return self.socket.fileno()

    def do_read(self, fileno):
        if fileno == self.socket.fileno():
            (client_socket, client_address) = self.socket.accept()
            self.poller.add(self, client_socket.fileno())
            self.client_sockets[client_socket.fileno()] = client_socket
        else:
            data, sender = self.client_sockets[fileno].recvfrom(4096)
            if not data:
                self.poller.remove(self, fileno)
                del (self.client_sockets[fileno])
            else:
                self.handle_request(data, sender, self.client_sockets[fileno])

    def handle_request(self, data, sender, socket):
        pass

    def get_name(self):
        return "unknown"

    def respond_to_search(self, destination, search_target):
        # dbg("Responding to search for %s" % self.get_name())
        date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
        location_url = self.root_url % {'ip_address': self.ip_address, 'port': self.port}
        message = ('HTTP/1.1 200 OK\r\n'
                   'CACHE-CONTROL: max-age=86400\r\n'
                   'DATE: %s\r\n'
                   'EXT:\r\n'
                   'LOCATION: %s\r\n'
                   'OPT: \"http://schemas.upnp.org/upnp/1/0/\"; ns=01\r\n'
                   '01-NLS: %s\r\n'
                   'SERVER: %s\r\n'
                   'ST: %s\r\n'
                   'USN: uuid:%s::%s\r\n' % (
                   date_str, location_url, self.uuid, self.server_version, search_target, self.persistent_uuid,
                   search_target))
        if self.other_headers:
            for header in self.other_headers:
                message += "%s\r\n" % header
        message += "\r\n"
        temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        message = bytes(message, 'utf-8')
        # dbg("Responding with: " + str(message) + "\n")
        temp_socket.sendto(message, destination)


# This subclass does the bulk of the work to mimic a WeMo switch on the network.

class fauxmo(upnp_device):
    @staticmethod
    def make_uuid(name):
        return ''.join(["%x" % sum([ord(c) for c in name])] + ["%x" % ord(c) for c in "%sfauxmo!" % name])[:14]

    def __init__(self, name, listener, poller, ip_address, port, action_handler=None):
        self.binaryState = 0
        self.serial = self.make_uuid(name)
        self.name = name
        self.ip_address = ip_address
        persistent_uuid = "Socket-1_0-" + self.serial
        other_headers = ['X-User-Agent: redsonic']
        upnp_device.__init__(self, listener, poller, port, "http://%(ip_address)s:%(port)s/setup.xml",
                             "Unspecified, UPnP/1.0, Unspecified", persistent_uuid, other_headers=other_headers,
                             ip_address=ip_address)
        if action_handler:
            self.action_handler = action_handler
        else:
            self.action_handler = self
        dbg("FauxMo device '%s' ready on %s:%s" % (self.name, self.ip_address, self.port))

    def get_name(self):
        return self.name

    def handle_request(self, data, sender, socket):
        if data.find(b'GET /setup.xml HTTP/1.1') == 0:
            # dbg("Receiving " + str(data) + "\n")
            # dbg("Responding to setup.xml for %s" % self.name)
            xml = SETUP_XML % {'device_name': self.name, 'device_serial': self.serial}
            location_url = self.root_url % {'ip_address': self.ip_address, 'port': self.port}
            date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
            message = ("HTTP/1.1 200 OK\r\n"
                       "CONTENT-LENGTH: %d\r\n"
                       "CONTENT-TYPE: text/xml\r\n"
                       "DATE: %s\r\n"
                       "LAST-MODIFIED: Sat, 01 Jan 2000 00:01:15 GMT\r\n"
                       "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                       "X-User-Agent: redsonic\r\n"
                       "LOCATION: %s\r\n"
                       "CONNECTION: close\r\n"
                       "\r\n"
                       "%s" % (len(xml), date_str, location_url,xml))
            message = bytes(message, 'utf-8')
            dbg("Responding with " + str(message) + "\n")
            socket.send(message)
        elif data.find(b'SOAPACTION: "urn:Belkin:service:basicevent:1#SetBinaryState"') != -1:
            success = False
            if data.find(b'<BinaryState>1</BinaryState>') != -1:
                # on
                dbg("Responding to ON for %s" % self.name)
                success = self.action_handler.on()
                if success:
                    self.binaryState = 1
            elif data.find(b'<BinaryState>0</BinaryState>') != -1:
                # off
                # dbg("Responding to OFF for %s" % self.name)
                success = self.action_handler.off()
                if success:
                    self.binaryState = 0
            else:
                dbg("Unknown Binary State request:")
                dbg(data)
            if success:
                xml = GETBINARY_XML % {'binary_state': self.binaryState}
                message = ("HTTP/1.1 200 OK\r\n"
                           "CONTENT-LENGTH: %d\r\n"
                           "CONTENT-TYPE: text/xml charset=\"utf-8\"\r\n"
                           "\r\n"
                           "%s" % (len(xml), xml))
                message = bytes(message, 'utf-8')
                socket.send(message)

        elif data.find(b'SOAPACTION: "urn:Belkin:service:basicevent:1#GetBinaryState"') != -1:
            dbg("Receiving " + str(data) + "\n")
            dbg("Responding to GetBinaryState for %s" % self.name)
            xml = GETBINARY_XML % {'binary_state': self.binaryState}
            message = ("HTTP/1.1 200 OK\r\n"
                       "CONTENT-LENGTH: %d\r\n"
                       "CONTENT-TYPE: text/xml; charset=\"utf-8\"\r\n"
                       "\r\n"
                       "%s" % (len(xml), xml))
            message = bytes(message, 'utf-8')
            dbg("Responding with " + str(message) + "\n")
            socket.send(message)
        else:
            dbg(data)

    def on(self):
        self.binaryState = 1
        return False

    def off(self):
        self.binaryState = 0
        return True


# Since we have a single process managing several virtual UPnP devices,
# we only need a single listener for UPnP broadcasts. When a matching
# search is received, it causes each device instance to respond.
#
# Note that this is currently hard-coded to recognize only the search
# from the Amazon Echo for WeMo devices. In particular, it does not
# support the more common root device general search. The Echo
# doesn't search for root devices.

class upnp_broadcast_responder(object):
    TIMEOUT = 0

    def __init__(self):
        self.devices = []

    def init_socket(self):
        ok = True
        self.ip = '239.255.255.250'
        self.port = 1900
        try:
            # This is needed to join a multicast group
            self.mreq = struct.pack("4sl", socket.inet_aton(self.ip), socket.INADDR_ANY)

            # Set up server socket
            self.ssock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.ssock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                self.ssock.bind(('', self.port))
            except Exception as e:
                dbg("WARNING: Failed to bind %s:%d: %s", (self.ip, self.port, e))
                ok = False

            try:
                self.ssock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, self.mreq)
            except Exception as e:
                dbg('WARNING: Failed to join multicast group:', e)
                ok = False

        except Exception as e:
            dbg("Failed to initialize UPnP sockets:", e)
            return False
        if ok:
            dbg("Listening for UPnP broadcasts")

    def fileno(self):
        return self.ssock.fileno()

    def do_read(self, fileno):
        data, sender = self.recvfrom(1024)
        if data:
            if not data.find(b"NOTIFY *") == 0: # I had bunch of other devices that were sending NOTIFY * and it was annoying, So you can remove it if it doesn't happens with you
                dbg("Receiving " + str(data) + "\n")
            if data.find(b'M-SEARCH') == 0 and data.find(b'upnp:rootdevice') != -1: # upnp:rootdevice # urn:Belkin:device:**
                for device in self.devices:
                    time.sleep(0.1)
                    device.respond_to_search(sender, 'urn:Belkin:device:**')
            else:
                pass

    # Receive network data
    def recvfrom(self, size):
        if self.TIMEOUT:
            self.ssock.setblocking(0)
            ready = select.select([self.ssock], [], [], self.TIMEOUT)[0]
        else:
            self.ssock.setblocking(1)
            ready = True

        try:
            if ready:
                return self.ssock.recvfrom(size)
            else:
                return False, False
        except Exception as e:
            dbg(e)
            return False, False

    def add_device(self, device):
        self.devices.append(device)
        dbg("UPnP broadcast listener: new device registered")


# This is an example handler class. The fauxmo class expects handlers to be
# instances of objects that have on() and off() methods that return True
# on success and False otherwise.
#
# This example class takes two full URLs that should be requested when an on
# and off command are invoked respectively. It ignores any return data.

class rest_api_handler(object):
    def __init__(self, on_cmd, off_cmd):
        self.on_cmd = on_cmd
        self.off_cmd = off_cmd

    def on(self):
        r = requests.get(self.on_cmd)
        return r.status_code == 200

    def off(self):
        r = requests.get(self.off_cmd)
        return r.status_code == 200


class dummy_handler(object):
    def __init__(self, name):
        self.name = name

    def on(self):
        print(self.name, "ON")
        return True

    def off(self):
        print(self.name, "OFF")
        return True


# Each entry is a list with the following elements:
#
# name of the virtual switch
# object with 'on' and 'off' methods
# port # (optional; may be omitted)

# NOTE: As of 2015-08-17, the Echo appears to have a hard-coded limit of
# 16 switches it can control. Only the first 16 elements of the FAUXMOS
# list will be used.

FAUXMOS = [ # 54001 and 54002 are ports
    ['office lights', dummy_handler('office lights'), 54001],
    ['kitchen lights', dummy_handler('kitchen lights'),54002],
]

if len(sys.argv) > 1 and sys.argv[1] == '-d':
    DEBUG = True

# Set up our singleton for polling the sockets for data ready
p = poller()

# Set up our singleton listener for UPnP broadcasts
u = upnp_broadcast_responder()
u.init_socket()

# Add the UPnP broadcast listener to the poller so we can respond
# when a broadcast is received.
p.add(u)

# Create our FauxMo virtual switch devices
for one_faux in FAUXMOS:
    if len(one_faux) == 2:
        # a fixed port wasn't specified, use a dynamic one
        one_faux.append(0)
    switch = fauxmo(one_faux[0], u, p, None, one_faux[2], action_handler=one_faux[1])

dbg("Entering main loop\n")

while True:
    # Allow time for a ctrl-c to stop the process
    p.poll(100)
    time.sleep(0.1)

