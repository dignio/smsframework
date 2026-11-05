import unittest
import threading
from datetime import datetime

from flask import Flask
from werkzeug.serving import make_server

from smsframework import Gateway, exc
from smsframework.providers import ForwardClientProvider, ForwardServerProvider, LoopbackProvider
from smsframework import OutgoingMessage, IncomingMessage
from smsframework.providers.forward.provider import jsonex_dumps, jsonex_loads





class ForwardProviderTest(unittest.TestCase):
    """ Test ForwardClientProvider and ForwardServerProvider """

    def _makeServer(self):
        """ Create a Flask app served by a Werkzeug server on a free local port

        :rtype: (flask.Flask, werkzeug.serving.BaseWSGIServer)
        """
        app = Flask(__name__)
        app.debug = True
        app.testing = True
        server = make_server('localhost', 0, app, threaded=False, passthrough_errors=True)
        return app, server

    def _startServer(self, app, server, gw):
        """ Register gateway receivers on the app and serve it in a thread

        :type gw: smsframework.Gateway.Gateway
        """
        gw.receiver_blueprints_register(app, prefix='/sms')
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        return thread

    def setUp(self):
        # Init servers first: ports are allocated dynamically
        app_client, self.srv_client = self._makeServer()
        app_server, self.srv_server = self._makeServer()

        # Init: client gateway
        # This "client" sends messages through a remote server
        self.gw_client = Gateway()
        self.gw_client.add_provider('fwd', ForwardClientProvider, server_url='http://a:b@localhost:{}/sms/fwd'.format(self.srv_server.port))

        # Init: server gateway
        # This "server" receives messages from an external SMS server and forwards them to the client
        self.gw_server = Gateway()
        self.gw_server.add_provider('lo', LoopbackProvider)
        self.gw_server.add_provider('fwd', ForwardServerProvider, clients=['http://a:b@localhost:{}/sms/fwd'.format(self.srv_client.port)])
        self.lo = self.gw_server.get_provider('lo')  # This is run in another thread, but we should have access to it
        ' :type: LoopbackProvider '

        # Run client & server in threads
        self.t_client = self._startServer(app_client, self.srv_client, self.gw_client)
        self.t_server = self._startServer(app_server, self.srv_server, self.gw_server)

    def tearDown(self):
        # Stop servers & threads
        for server, thread in ((self.srv_client, self.t_client), (self.srv_server, self.t_server)):
            server.shutdown()
            thread.join()
            server.server_close()

    def test_jsonex(self):
        """ Test de/coding messages """
        ### OutgoingMessage
        om_in = OutgoingMessage('+123', 'Test', '+987', 'fwd')
        om_in.options(allow_reply=True)
        # Encode, Decode
        j = jsonex_dumps(om_in)
        om_out = jsonex_loads(j)
        """ :type om_out: OutgoingMessage """
        # Check
        self.assertEqual(om_out.dst, '123')
        self.assertEqual(om_out.src, om_in.src)
        self.assertEqual(om_out.body, om_in.body)
        self.assertEqual(om_out.provider, om_in.provider)
        self.assertEqual(om_out.meta, None)
        self.assertEqual(om_out.provider_options.allow_reply, om_in.provider_options.allow_reply)
        self.assertEqual(om_out.provider_params, {})

        ### IncomingMessage
        im_in = IncomingMessage('+123', 'Test', 'abc123def', '+987', datetime(2019,1,1,15,0,0,875), {'a':1})
        # Encode, Decode
        j = jsonex_dumps(im_in)
        im_out = jsonex_loads(j)
        """ :type im_out: IncomingMessage """
        # Check
        self.assertEqual(im_out.src, im_in.src)
        self.assertEqual(im_out.body, im_in.body)
        self.assertEqual(im_out.msgid, im_in.msgid)
        self.assertEqual(im_out.dst, im_in.dst)
        self.assertEqual(im_out.rtime, im_in.rtime)
        self.assertEqual(im_out.meta, im_in.meta)


    def testSend(self):
        """ Send messages """

        # Send a message
        om = OutgoingMessage('+1234', 'Hi man!').options(senderId='me').params(a=1).route(1, 2, 3)
        rom = self.gw_client.send(om)

        # Check traffic
        traffic = self.lo.get_traffic()
        self.assertEqual(len(traffic), 1)
        tom = traffic.pop()

        for m in (om, rom, tom):
            self.assertEqual(m.src, None)
            self.assertEqual(m.dst, '1234')
            self.assertEqual(m.body, 'Hi man!')
            self.assertEqual(m.provider, 'lo')  # Remote provider should be exposed
            self.assertEqual(m.provider_options.senderId, 'me')
            self.assertEqual(m.provider_params, {'a': 1})
            self.assertEqual(m.routing_values, [1, 2, 3])
            self.assertEqual(m.msgid, '1')
            self.assertEqual(m.meta, None)

    def testReceive(self):
        """ Receive messages """

        # Message receiver
        received = []
        def onReceive(message): received.append(message)
        self.gw_client.onReceive += onReceive

        # Receive a message
        self.lo.received('1111', 'Yo')

        # Check
        self.assertEqual(len(received), 1)
        msg = received.pop()
        ':type: IncomingMessage'

        self.assertEqual(msg.msgid, 1)
        self.assertEqual(msg.src, '1111')
        self.assertEqual(msg.body, 'Yo')
        self.assertEqual(msg.dst, None)
        self.assertIsInstance(msg.rtime, datetime)
        self.assertEqual(msg.meta, {})
        self.assertEqual(msg.provider, 'lo')  # Remote provider should be exposed

    def testStatus(self):
        """ Receive statuses """

        # Status receiver
        statuses = []
        def onStatus(status): statuses.append(status)
        self.gw_client.onStatus += onStatus

        # Subscriber
        incoming = []
        def subscriber(message): incoming.append(message)
        self.lo.subscribe('1234', subscriber)

        # Send a message, request status report
        om = OutgoingMessage('+1234', 'Hi man!').options(status_report=True)
        rom = self.gw_client.send(om)

        # Check
        self.assertEqual(len(statuses), 1)
        status = statuses.pop()
        ':type: MessageStatus'
        self.assertEqual(status.msgid, '1')
        self.assertIsInstance(status.rtime, datetime)
        self.assertEqual(status.provider, 'lo')
        self.assertEqual(status.accepted, True)
        self.assertEqual(status.delivered, True)
        self.assertEqual(status.expired, False)
        self.assertEqual(status.error, False)
        self.assertEqual(status.status_code, None)
        self.assertEqual(status.status, 'OK')
        self.assertEqual(status.meta, {})

    def testServerError(self):
        """ Test how errors are transferred from the server """

        # Erroneous subscribers
        def tired_subscriber(message):
            raise OverflowError('Tired')
        self.lo.subscribe('1234', tired_subscriber)

        def offline_subscriber(message):
            raise exc.ServerError('Offline')
        self.lo.subscribe('5678', offline_subscriber)

        # Send: 1
        om = OutgoingMessage('+1234', 'Hi man!')
        self.assertRaises(RuntimeError, self.gw_client.send, om)  # Unknown error classes are converted to RuntimeError

        # Send: 2
        om = OutgoingMessage('+5678', 'Hi man!')
        self.assertRaises(exc.ServerError, self.gw_client.send, om)  # Known errors: as is

    def testClientError(self):
        """ Test how server behaves when the client cannot receive """

        # Message receiver
        def failing_receiver(message):
            print(message)
            raise OverflowError(':(')
        self.gw_client.onReceive += failing_receiver

        # Receive a message
        self.assertRaises(RuntimeError, self.lo.received, '1111', 'Yo')
