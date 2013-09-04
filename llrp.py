from __future__ import print_function
import time
import socket
import logging
import struct
import Queue
from threading import Thread, Condition
from llrp_proto import LLRPROSpec, LLRPError, Message_struct, Message_Type2Name
import copy
from util import *
from twisted.internet import reactor
from twisted.internet.protocol import Protocol, ClientFactory, ClientCreator

LLRP_PORT = 5084

class LLRPMessage:
    hdr_fmt = '!HI'
    hdr_len = struct.calcsize(hdr_fmt)
    full_hdr_fmt = hdr_fmt + 'I'
    full_hdr_len = struct.calcsize(full_hdr_fmt)
    msgdict = None
    msgbytes = None

    def __init__ (self, msgdict=None, msgbytes=None):
        if not (msgdict or msgbytes):
            raise LLRPError('Provide either a message dict or a sequence' \
                    ' of bytes.')
        if msgdict:
            self.msgdict = dict(msgdict)
        if msgbytes:
            self.msgbytes = copy.copy(msgbytes)

    def serialize (self):
        if self.msgdict is None:
            raise LLRPError('No message dict to serialize.')
        name = self.msgdict.keys()[0]
        logging.debug('serializing {} command'.format(name))
        ver = self.msgdict[name]['Ver'] & BITMASK(3)
        msgtype = self.msgdict[name]['Type'] & BITMASK(10)
        msgid = self.msgdict[name]['ID']
        try:
            encoder = Message_struct[name]['encode']
        except KeyError:
            raise LLRPError('Cannot find encoder for message type '
                    '{}'.format(name))
        data = encoder(self.msgdict[name])
        self.msgbytes = struct.pack(self.full_hdr_fmt,
                (ver << 10) | msgtype,
                len(data) + self.full_hdr_len,
                msgid) + data
        logging.debug('serialized bytes: {}'.format(hexlify(self.msgbytes)))
        logging.debug('done serializing {} command'.format(name))
        return self.msgbytes

    def deserialize (self):
        if self.msgbytes is None:
            raise LLRPError('No message bytes to deserialize.')
        data = ''.join(self.msgbytes)
        msgtype, length, msgid = struct.unpack(self.full_hdr_fmt,
                data[:self.full_hdr_len])
        ver = (msgtype >> 10) & BITMASK(3)
        msgtype = msgtype & BITMASK(10)
        try:
            name = Message_Type2Name[msgtype]
            logging.debug('deserializing {} command'.format(name))
            decoder = Message_struct[name]['decode']
        except KeyError:
            raise LLRPError('Cannot find decoder for message type '
                    '{}'.format(msgtype))
        body = data[self.full_hdr_len:length]
        try:
            self.msgdict = {
               name: dict(decoder(body))
            }
            self.msgdict[name]['Ver'] = ver
            self.msgdict[name]['Type'] = msgtype
            self.msgdict[name]['ID'] = msgid
        except LLRPError as e:
            logging.warning('Problem with {} message format: {}'.format(name, e))
        logging.debug('done deserializing {} command'.format(name))
        return self.msgdict

class LLRPClient (Protocol):
    def connectionMade(self):
        logging.debug('socket connected')

    def connectionLost(self, reason):
        logging.debug('socket closed: {}'.format(reason))

    def dataReceived (self, data):
        #msgbytes = self.recv(LLRPMessage.hdr_len)
        #bytes_read = len(msgbytes)
        #ty, length = struct.unpack(LLRPMessage.hdr_fmt, msgbytes)
        #to_read = length - LLRPMessage.hdr_len
        #while (bytes_read < to_read):
        #    bs = self.recv(to_read)
        #    bytes_read += len(bs)
        #    msgbytes += bs
        logging.debug('Got {} bytes from reader: {}'.format(len(data),
                    data.encode('hex')))
        #self.inqueue.put(LLRPMessage(msgbytes=data).deserialize())

    def sendLLRPMessage (self, llrp_msg):
        reactor.callFromThread(self.sendMessage, llrp_msg.serialize())

    def sendMessage (self, msg):
        self.transport.write(msg)

class LLRPClientFactory (ClientFactory):
    protocol = LLRPClient

    def clientConnectionFailed (self, connector, reason):
        logging.debug('connection failed: {}'.format(reason))
        reactor.stop()

    def clientConnectionLost (self, connector, reason):
        logging.debug('connection lost: {}'.format(reason))
        reactor.stop()

class LLRPReaderThread (Thread):
    """ Thread object that connects input and output message queues to a
        socket."""
    client = None
    rospec = None
    host = None
    port = None
    protocol = None

    def __init__ (self, host, port=LLRP_PORT):
        super(LLRPReaderThread, self).__init__()
        self.host = host
        self.port = port

    def cbConnected (self, connectedProtocol):
        logging.debug('connected!')
        self.protocol = connectedProtocol

    def ebConnectError (self, reason):
        logging.debug('connection error: {}'.format(reason))
        pass

    def run (self):
        logging.debug('will connect to {}:{}'.format(self.host, self.port))
        cc = ClientCreator(reactor, LLRPClient)
        whenConnected = cc.connectTCP(self.host, self.port)
        #reactor.run(installSignalHandlers=0)
        whenConnected.addCallbacks(self.cbConnected, self.ebConnectError)
        reactor.run(False)

    def start_inventory (self):
        "Start the reader inventorying."
        if not self.protocol:
            return
        if not self.rospec:
            # create an ROSpec, which defines the reader's inventorying
            # behavior, and start running it on the reader
            self.rospec = LLRPROSpec(1)

        roSpecId = self.rospec['ROSpec']['ROSpecID']

        # add an ROspec
        self.protocol.sendLLRPMessage(LLRPMessage(msgdict={
            'ADD_ROSPEC': {
                'Ver':  1,
                'Type': 20,
                'ID':   0,
                'ROSpecID': roSpecId,
                'ROSpec': self.rospec['ROSpec'],
            }}))

        # enable the ROspec
        self.protocol.sendLLRPMessage(LLRPMessage(msgdict={
            'ENABLE_ROSPEC': {
                'Ver':  1,
                'Type': 24,
                'ID':   0,
                'ROSpecID': roSpecId
            }}))

        # start the ROspec
        self.protocol.sendLLRPMessage(LLRPMessage(msgdict={
            'START_ROSPEC': {
                'Ver':  1,
                'Type': 22,
                'ID':   0,
                'ROSpecID': roSpecId
            }}))

    def stop_inventory (self):
        "Stop the reader from inventorying."
        if not self.protocol:
            return
        if not self.rospec:
            return

        roSpecId = self.rospec['ROSpec']['ROSpecID']

        # stop the ROspec
        self.protocol.sendLLRPMessage(LLRPMessage(msgdict={
            'STOP_ROSPEC': {
                'Ver':  1,
                'Type': 23,
                'ID':   0,
                'ROSpecID': roSpecId
            }}))

        # disable the ROspec
        self.protocol.sendLLRPMessage(LLRPMessage(msgdict={
            'DISABLE_ROSPEC': {
                'Ver':  1,
                'Type': 25,
                'ID':   0,
                'ROSpecID': roSpecId
            }}))

        # delete the ROspec
        self.protocol.sendLLRPMessage(LLRPMessage(msgdict={
            'DELETE_ROSPEC': {
                'Ver':  1,
                'Type': 21,
                'ID':   0,
                'ROSpecID': roSpecId
            }}))

    def disconnect (self):
        logging.debug('stopping reactor')
        reactor.callFromThread(reactor.stop)