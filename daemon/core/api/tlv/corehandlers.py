"""
socket server request handlers leveraged by core servers.
"""

import logging
import os
from queue import Queue, Empty
import shlex
import shutil
import socketserver
import sys
import threading
import time
from builtins import range
from itertools import repeat

from core import utils
from core.api.tlv import coreapi, dataconversion, structutils
from core.config import ConfigShim
from core.emulator.data import ConfigData, ExceptionData
from core.emulator.data import EventData
from core.emulator.data import FileData
from core.emulator.emudata import InterfaceData
from core.emulator.emudata import LinkOptions
from core.emulator.emudata import NodeOptions
from core.emulator.enumerations import ConfigDataTypes
from core.emulator.enumerations import ConfigFlags
from core.emulator.enumerations import ConfigTlvs
from core.emulator.enumerations import EventTlvs
from core.emulator.enumerations import EventTypes
from core.emulator.enumerations import ExceptionTlvs
from core.emulator.enumerations import ExecuteTlvs
from core.emulator.enumerations import FileTlvs
from core.emulator.enumerations import LinkTlvs
from core.emulator.enumerations import LinkTypes
from core.emulator.enumerations import MessageFlags
from core.emulator.enumerations import MessageTypes
from core.emulator.enumerations import NodeTlvs
from core.emulator.enumerations import NodeTypes
from core.emulator.enumerations import RegisterTlvs
from core.emulator.enumerations import SessionTlvs
from core.nodes import nodeutils
from core.services.coreservices import ServiceManager
from core.services.coreservices import ServiceShim


class CoreHandler(socketserver.BaseRequestHandler):
    """
    The CoreHandler class uses the RequestHandler class for servicing requests.
    """

    def __init__(self, request, client_address, server):
        """
        Create a CoreRequestHandler instance.

        :param request: request object
        :param str client_address: client address
        :param CoreServer server: core server instance
        """
        self.done = False
        self.message_handlers = {
            MessageTypes.NODE.value: self.handle_node_message,
            MessageTypes.LINK.value: self.handle_link_message,
            MessageTypes.EXECUTE.value: self.handle_execute_message,
            MessageTypes.REGISTER.value: self.handle_register_message,
            MessageTypes.CONFIG.value: self.handle_config_message,
            MessageTypes.FILE.value: self.handle_file_message,
            MessageTypes.INTERFACE.value: self.handle_interface_message,
            MessageTypes.EVENT.value: self.handle_event_message,
            MessageTypes.SESSION.value: self.handle_session_message,
        }
        self.message_queue = Queue()
        self.node_status_request = {}
        self._shutdown_lock = threading.Lock()
        self._sessions_lock = threading.Lock()

        self.handler_threads = []
        num_threads = int(server.config["numthreads"])
        if num_threads < 1:
            raise ValueError("invalid number of threads: %s" % num_threads)

        logging.debug("launching core server handler threads: %s", num_threads)
        for _ in range(num_threads):
            thread = threading.Thread(target=self.handler_thread)
            self.handler_threads.append(thread)
            thread.start()

        self.master = False
        self.session = None

        # core emulator
        self.coreemu = server.coreemu

        utils.close_onexec(request.fileno())
        socketserver.BaseRequestHandler.__init__(self, request, client_address, server)

    def setup(self):
        """
        Client has connected, set up a new connection.

        :return: nothing
        """
        logging.debug("new TCP connection: %s", self.client_address)

    def finish(self):
        """
        Client has disconnected, end this request handler and disconnect
        from the session. Shutdown sessions that are not running.

        :return: nothing
        """
        logging.debug("finishing request handler")
        logging.debug("remaining message queue size: %s", self.message_queue.qsize())

        # give some time for message queue to deplete
        timeout = 10
        wait = 0
        while not self.message_queue.empty():
            logging.debug("waiting for message queue to empty: %s seconds", wait)
            time.sleep(1)
            wait += 1
            if wait == timeout:
                logging.warning("queue failed to be empty, finishing request handler")
                break

        logging.info("client disconnected: notifying threads")
        self.done = True
        for thread in self.handler_threads:
            logging.info("waiting for thread: %s", thread.getName())
            thread.join(timeout)
            if thread.isAlive():
                logging.warning("joining %s failed: still alive after %s sec", thread.getName(), timeout)

        logging.info("connection closed: %s", self.client_address)
        if self.session:
            # remove client from session broker and shutdown if there are no clients
            self.remove_session_handlers()
            self.session.broker.session_clients.remove(self)
            if not self.session.broker.session_clients and not self.session.is_active():
                logging.info("no session clients left and not active, initiating shutdown")
                self.coreemu.delete_session(self.session.id)

        return socketserver.BaseRequestHandler.finish(self)

    def session_message(self, flags=0):
        """
        Build CORE API Sessions message based on current session info.

        :param int flags: message flags
        :return: session message
        """
        id_list = []
        name_list = []
        file_list = []
        node_count_list = []
        date_list = []
        thumb_list = []
        num_sessions = 0

        with self._sessions_lock:
            for _id in self.coreemu.sessions:
                session = self.coreemu.sessions[_id]
                num_sessions += 1
                id_list.append(str(_id))

                name = session.name
                if not name:
                    name = ""
                name_list.append(name)

                file_name = session.file_name
                if not file_name:
                    file_name = ""
                file_list.append(file_name)

                node_count_list.append(str(session.get_node_count()))

                date_list.append(time.ctime(session._state_time))

                thumb = session.thumbnail
                if not thumb:
                    thumb = ""
                thumb_list.append(thumb)

        session_ids = "|".join(id_list)
        names = "|".join(name_list)
        files = "|".join(file_list)
        node_counts = "|".join(node_count_list)
        dates = "|".join(date_list)
        thumbs = "|".join(thumb_list)

        if num_sessions > 0:
            tlv_data = b""
            if len(session_ids) > 0:
                tlv_data += coreapi.CoreSessionTlv.pack(SessionTlvs.NUMBER.value, session_ids)
            if len(names) > 0:
                tlv_data += coreapi.CoreSessionTlv.pack(SessionTlvs.NAME.value, names)
            if len(files) > 0:
                tlv_data += coreapi.CoreSessionTlv.pack(SessionTlvs.FILE.value, files)
            if len(node_counts) > 0:
                tlv_data += coreapi.CoreSessionTlv.pack(SessionTlvs.NODE_COUNT.value, node_counts)
            if len(dates) > 0:
                tlv_data += coreapi.CoreSessionTlv.pack(SessionTlvs.DATE.value, dates)
            if len(thumbs) > 0:
                tlv_data += coreapi.CoreSessionTlv.pack(SessionTlvs.THUMB.value, thumbs)
            message = coreapi.CoreSessionMessage.pack(flags, tlv_data)
        else:
            message = None

        return message

    def handle_broadcast_event(self, event_data):
        """
        Callback to handle an event broadcast out from a session.

        :param core.data.EventData event_data: event data to handle
        :return: nothing
        """
        logging.debug("handling broadcast event: %s", event_data)

        tlv_data = structutils.pack_values(coreapi.CoreEventTlv, [
            (EventTlvs.NODE, event_data.node),
            (EventTlvs.TYPE, event_data.event_type),
            (EventTlvs.NAME, event_data.name),
            (EventTlvs.DATA, event_data.data),
            (EventTlvs.TIME, event_data.time),
            (EventTlvs.SESSION, event_data.session)
        ])
        message = coreapi.CoreEventMessage.pack(0, tlv_data)

        try:
            self.sendall(message)
        except IOError:
            logging.exception("error sending event message")

    def handle_broadcast_file(self, file_data):
        """
        Callback to handle a file broadcast out from a session.

        :param core.data.FileData file_data: file data to handle
        :return: nothing
        """
        logging.debug("handling broadcast file: %s", file_data)

        tlv_data = structutils.pack_values(coreapi.CoreFileTlv, [
            (FileTlvs.NODE, file_data.node),
            (FileTlvs.NAME, file_data.name),
            (FileTlvs.MODE, file_data.mode),
            (FileTlvs.NUMBER, file_data.number),
            (FileTlvs.TYPE, file_data.type),
            (FileTlvs.SOURCE_NAME, file_data.source),
            (FileTlvs.SESSION, file_data.session),
            (FileTlvs.DATA, file_data.data),
            (FileTlvs.COMPRESSED_DATA, file_data.compressed_data),
        ])
        message = coreapi.CoreFileMessage.pack(file_data.message_type, tlv_data)

        try:
            self.sendall(message)
        except IOError:
            logging.exception("error sending file message")

    def handle_broadcast_config(self, config_data):
        """
        Callback to handle a config broadcast out from a session.

        :param core.data.ConfigData config_data: config data to handle
        :return: nothing
        """
        logging.debug("handling broadcast config: %s", config_data)
        message = dataconversion.convert_config(config_data)
        try:
            self.sendall(message)
        except IOError:
            logging.exception("error sending config message")

    def handle_broadcast_exception(self, exception_data):
        """
        Callback to handle an exception broadcast out from a session.

        :param core.data.ExceptionData exception_data: exception data to handle
        :return: nothing
        """
        logging.debug("handling broadcast exception: %s", exception_data)
        tlv_data = structutils.pack_values(coreapi.CoreExceptionTlv, [
            (ExceptionTlvs.NODE, exception_data.node),
            (ExceptionTlvs.SESSION, exception_data.session),
            (ExceptionTlvs.LEVEL, exception_data.level),
            (ExceptionTlvs.SOURCE, exception_data.source),
            (ExceptionTlvs.DATE, exception_data.date),
            (ExceptionTlvs.TEXT, exception_data.text)
        ])
        message = coreapi.CoreExceptionMessage.pack(0, tlv_data)

        try:
            self.sendall(message)
        except IOError:
            logging.exception("error sending exception message")

    def handle_broadcast_node(self, node_data):
        """
        Callback to handle an node broadcast out from a session.

        :param core.data.NodeData node_data: node data to handle
        :return: nothing
        """
        logging.debug("handling broadcast node: %s", node_data)
        message = dataconversion.convert_node(node_data)
        try:
            self.sendall(message)
        except IOError:
            logging.exception("error sending node message")

    def handle_broadcast_link(self, link_data):
        """
        Callback to handle an link broadcast out from a session.

        :param core.data.LinkData link_data: link data to handle
        :return: nothing
        """
        logging.debug("handling broadcast link: %s", link_data)
        per = ""
        if link_data.per is not None:
            per = str(link_data.per)

        tlv_data = structutils.pack_values(coreapi.CoreLinkTlv, [
            (LinkTlvs.N1_NUMBER, link_data.node1_id),
            (LinkTlvs.N2_NUMBER, link_data.node2_id),
            (LinkTlvs.DELAY, link_data.delay),
            (LinkTlvs.BANDWIDTH, link_data.bandwidth),
            (LinkTlvs.PER, per),
            (LinkTlvs.DUP, link_data.dup),
            (LinkTlvs.JITTER, link_data.jitter),
            (LinkTlvs.MER, link_data.mer),
            (LinkTlvs.BURST, link_data.burst),
            (LinkTlvs.SESSION, link_data.session),
            (LinkTlvs.MBURST, link_data.mburst),
            (LinkTlvs.TYPE, link_data.link_type),
            (LinkTlvs.GUI_ATTRIBUTES, link_data.gui_attributes),
            (LinkTlvs.UNIDIRECTIONAL, link_data.unidirectional),
            (LinkTlvs.EMULATION_ID, link_data.emulation_id),
            (LinkTlvs.NETWORK_ID, link_data.network_id),
            (LinkTlvs.KEY, link_data.key),
            (LinkTlvs.INTERFACE1_NUMBER, link_data.interface1_id),
            (LinkTlvs.INTERFACE1_NAME, link_data.interface1_name),
            (LinkTlvs.INTERFACE1_IP4, link_data.interface1_ip4),
            (LinkTlvs.INTERFACE1_IP4_MASK, link_data.interface1_ip4_mask),
            (LinkTlvs.INTERFACE1_MAC, link_data.interface1_mac),
            (LinkTlvs.INTERFACE1_IP6, link_data.interface1_ip6),
            (LinkTlvs.INTERFACE1_IP6_MASK, link_data.interface1_ip6_mask),
            (LinkTlvs.INTERFACE2_NUMBER, link_data.interface2_id),
            (LinkTlvs.INTERFACE2_NAME, link_data.interface2_name),
            (LinkTlvs.INTERFACE2_IP4, link_data.interface2_ip4),
            (LinkTlvs.INTERFACE2_IP4_MASK, link_data.interface2_ip4_mask),
            (LinkTlvs.INTERFACE2_MAC, link_data.interface2_mac),
            (LinkTlvs.INTERFACE2_IP6, link_data.interface2_ip6),
            (LinkTlvs.INTERFACE2_IP6_MASK, link_data.interface2_ip6_mask),
            (LinkTlvs.OPAQUE, link_data.opaque)
        ])

        message = coreapi.CoreLinkMessage.pack(link_data.message_type, tlv_data)

        try:
            self.sendall(message)
        except IOError:
            logging.exception("error sending Event Message")

    def register(self):
        """
        Return a Register Message

        :return: register message data
        """
        logging.info("GUI has connected to session %d at %s", self.session.id, time.ctime())

        tlv_data = b""
        tlv_data += coreapi.CoreRegisterTlv.pack(RegisterTlvs.EXECUTE_SERVER.value, "core-daemon")
        tlv_data += coreapi.CoreRegisterTlv.pack(RegisterTlvs.EMULATION_SERVER.value, "core-daemon")
        tlv_data += coreapi.CoreRegisterTlv.pack(self.session.broker.config_type, self.session.broker.name)
        tlv_data += coreapi.CoreRegisterTlv.pack(self.session.location.config_type, self.session.location.name)
        tlv_data += coreapi.CoreRegisterTlv.pack(self.session.mobility.config_type, self.session.mobility.name)
        for model_name in self.session.mobility.models:
            model_class = self.session.mobility.models[model_name]
            tlv_data += coreapi.CoreRegisterTlv.pack(model_class.config_type, model_class.name)
        tlv_data += coreapi.CoreRegisterTlv.pack(self.session.services.config_type.value, self.session.services.name)
        tlv_data += coreapi.CoreRegisterTlv.pack(self.session.emane.config_type.value, self.session.emane.name)
        for model_name in self.session.emane.models:
            model_class = self.session.emane.models[model_name]
            tlv_data += coreapi.CoreRegisterTlv.pack(model_class.config_type, model_class.name)
        tlv_data += coreapi.CoreRegisterTlv.pack(self.session.options.config_type.value, self.session.options.name)
        tlv_data += coreapi.CoreRegisterTlv.pack(self.session.metadata.config_type.value, self.session.metadata.name)

        return coreapi.CoreRegMessage.pack(MessageFlags.ADD.value, tlv_data)

    def sendall(self, data):
        """
        Send raw data to the other end of this TCP connection
        using socket"s sendall().

        :param data: data to send over request socket
        :return: data sent
        """
        return self.request.sendall(data)

    def receive_message(self):
        """
        Receive data and return a CORE API message object.

        :return: received message
        :rtype: coreapi.CoreMessage
        """
        try:
            header = self.request.recv(coreapi.CoreMessage.header_len)
        except IOError as e:
            raise IOError("error receiving header (%s)" % e)

        if len(header) != coreapi.CoreMessage.header_len:
            if len(header) == 0:
                raise EOFError("client disconnected")
            else:
                raise IOError("invalid message header size")

        message_type, message_flags, message_len = coreapi.CoreMessage.unpack_header(header)
        if message_len == 0:
            logging.warning("received message with no data")

        data = b""
        while len(data) < message_len:
            data += self.request.recv(message_len - len(data))
            if len(data) > message_len:
                error_message = "received message length does not match received data (%s != %s)" % (
                    len(data), message_len)
                logging.error(error_message)
                raise IOError(error_message)

        try:
            message_class = coreapi.CLASS_MAP[message_type]
            message = message_class(message_flags, header, data)
        except KeyError:
            message = coreapi.CoreMessage(message_flags, header, data)
            message.message_type = message_type
            logging.exception("unimplemented core message type: %s", message.type_str())

        return message

    def queue_message(self, message):
        """
        Queue an API message for later processing.

        :param message: message to queue
        :return: nothing
        """
        logging.debug("queueing msg (queuedtimes = %s): type %s", message.queuedtimes, MessageTypes(
            message.message_type))
        self.message_queue.put(message)

    def handler_thread(self):
        """
        CORE API message handling loop that is spawned for each server
        thread; get CORE API messages from the incoming message queue,
        and call handlemsg() for processing.

        :return: nothing
        """
        while not self.done:
            try:
                message = self.message_queue.get(timeout=1)
                self.handle_message(message)
            except Empty:
                pass

    def handle_message(self, message):
        """
        Handle an incoming message; dispatch based on message type,
        optionally sending replies.

        :param message: message to handle
        :return: nothing
        """
        if self.session and self.session.broker.handle_message(message):
            logging.debug("message not being handled locally")
            return

        logging.debug("%s handling message:\n%s", threading.currentThread().getName(), message)

        if message.message_type not in self.message_handlers:
            logging.error("no handler for message type: %s", message.type_str())
            return

        message_handler = self.message_handlers[message.message_type]

        try:
            # TODO: this needs to be removed, make use of the broadcast message methods
            replies = message_handler(message)
            self.dispatch_replies(replies, message)
        except:
            logging.exception("%s: exception while handling message: %s", threading.currentThread().getName(), message)

    def dispatch_replies(self, replies, message):
        """
        Dispatch replies by CORE to message msg previously received from the client.

        :param list replies: reply messages to dispatch
        :param message: message for replies
        :return: nothing
        """
        logging.debug("dispatching replies: %s", replies)
        for reply in replies:
            message_type, message_flags, message_length = coreapi.CoreMessage.unpack_header(reply)
            try:
                reply_message = coreapi.CLASS_MAP[message_type](
                    message_flags,
                    reply[:coreapi.CoreMessage.header_len],
                    reply[coreapi.CoreMessage.header_len:]
                )
            except KeyError:
                # multiple TLVs of same type cause KeyError exception
                reply_message = "CoreMessage (type %d flags %d length %d)" % (
                    message_type, message_flags, message_length)

            logging.debug("dispatch reply:\n%s", reply_message)

            try:
                self.sendall(reply)
            except IOError:
                logging.exception("error dispatching reply")

    def handle(self):
        """
        Handle a new connection request from a client. Dispatch to the
        recvmsg() method for receiving data into CORE API messages, and
        add them to an incoming message queue.

        :return: nothing
        """
        # use port as session id
        port = self.request.getpeername()[1]

        # TODO: add shutdown handler for session
        self.session = self.coreemu.create_session(port, master=False)
        # self.session.shutdown_handlers.append(self.session_shutdown)
        logging.debug("created new session for client: %s", self.session.id)

        # TODO: hack to associate this handler with this sessions broker for broadcasting
        # TODO: broker needs to be pulled out of session to the server/handler level
        if self.master:
            logging.debug("session set to master")
            self.session.master = True
        self.session.broker.session_clients.append(self)

        # add handlers for various data
        self.add_session_handlers()

        # set initial session state
        self.session.set_state(EventTypes.DEFINITION_STATE)

        while True:
            try:
                message = self.receive_message()
            except EOFError:
                logging.info("client disconnected")
                break
            except IOError:
                logging.exception("error receiving message")
                break

            message.queuedtimes = 0
            self.queue_message(message)

            # delay is required for brief connections, allow session joining
            if message.message_type == MessageTypes.SESSION.value:
                time.sleep(0.125)

            # broadcast node/link messages to other connected clients
            if message.message_type not in [MessageTypes.NODE.value, MessageTypes.LINK.value]:
                continue

            for client in self.session.broker.session_clients:
                if client == self:
                    continue

                logging.debug("BROADCAST TO OTHER CLIENT: %s", client)
                client.sendall(message.raw_message)

    def send_exception(self, level, source, text, node=None):
        """
        Sends an exception for display within the GUI.

        :param core.enumerations.ExceptionLevel level: level for exception
        :param str source: source where exception came from
        :param str text: details about exception
        :param int node: node id, if related to a specific node
        :return:
        """
        exception_data = ExceptionData(
            session=str(self.session.id),
            node=node,
            date=time.ctime(),
            level=level.value,
            source=source,
            text=text
        )
        self.handle_broadcast_exception(exception_data)

    def add_session_handlers(self):
        logging.debug("adding session broadcast handlers")
        self.session.event_handlers.append(self.handle_broadcast_event)
        self.session.exception_handlers.append(self.handle_broadcast_exception)
        self.session.node_handlers.append(self.handle_broadcast_node)
        self.session.link_handlers.append(self.handle_broadcast_link)
        self.session.file_handlers.append(self.handle_broadcast_file)
        self.session.config_handlers.append(self.handle_broadcast_config)

    def remove_session_handlers(self):
        logging.debug("removing session broadcast handlers")
        self.session.event_handlers.remove(self.handle_broadcast_event)
        self.session.exception_handlers.remove(self.handle_broadcast_exception)
        self.session.node_handlers.remove(self.handle_broadcast_node)
        self.session.link_handlers.remove(self.handle_broadcast_link)
        self.session.file_handlers.remove(self.handle_broadcast_file)
        self.session.config_handlers.remove(self.handle_broadcast_config)

    def handle_node_message(self, message):
        """
        Node Message handler

        :param coreapi.CoreNodeMessage message: node message
        :return: replies to node message
        """
        replies = []
        if message.flags & MessageFlags.ADD.value and message.flags & MessageFlags.DELETE.value:
            logging.warning("ignoring invalid message: add and delete flag both set")
            return ()

        node_type = None
        node_type_value = message.get_tlv(NodeTlvs.TYPE.value)
        if node_type_value is not None:
            node_type = NodeTypes(node_type_value)

        node_id = message.get_tlv(NodeTlvs.NUMBER.value)

        node_options = NodeOptions(
            name=message.get_tlv(NodeTlvs.NAME.value),
            model=message.get_tlv(NodeTlvs.MODEL.value)
        )

        node_options.set_position(
            x=message.get_tlv(NodeTlvs.X_POSITION.value),
            y=message.get_tlv(NodeTlvs.Y_POSITION.value)
        )

        lat = message.get_tlv(NodeTlvs.LATITUDE.value)
        if lat is not None:
            lat = float(lat)
        lon = message.get_tlv(NodeTlvs.LONGITUDE.value)
        if lon is not None:
            lon = float(lon)
        alt = message.get_tlv(NodeTlvs.ALTITUDE.value)
        if alt is not None:
            alt = float(alt)
        node_options.set_location(lat=lat, lon=lon, alt=alt)

        node_options.icon = message.get_tlv(NodeTlvs.ICON.value)
        node_options.canvas = message.get_tlv(NodeTlvs.CANVAS.value)
        node_options.opaque = message.get_tlv(NodeTlvs.OPAQUE.value)

        services = message.get_tlv(NodeTlvs.SERVICES.value)
        if services:
            node_options.services = services.split("|")

        if message.flags & MessageFlags.ADD.value:
            node = self.session.add_node(node_type, node_id, node_options)
            if node:
                if message.flags & MessageFlags.STRING.value:
                    self.node_status_request[node.id] = True

                if self.session.state == EventTypes.RUNTIME_STATE.value:
                    self.send_node_emulation_id(node.id)
        elif message.flags & MessageFlags.DELETE.value:
            with self._shutdown_lock:
                result = self.session.delete_node(node_id)

                # if we deleted a node broadcast out its removal
                if result and message.flags & MessageFlags.STRING.value:
                    tlvdata = b""
                    tlvdata += coreapi.CoreNodeTlv.pack(NodeTlvs.NUMBER.value, node_id)
                    flags = MessageFlags.DELETE.value | MessageFlags.LOCAL.value
                    replies.append(coreapi.CoreNodeMessage.pack(flags, tlvdata))
        # node update
        else:
            self.session.update_node(node_id, node_options)

        return replies

    def handle_link_message(self, message):
        """
        Link Message handler

        :param coreapi.CoreLinkMessage message: link message to handle
        :return: link message replies
        """
        node_one_id = message.get_tlv(LinkTlvs.N1_NUMBER.value)
        node_two_id = message.get_tlv(LinkTlvs.N2_NUMBER.value)

        interface_one = InterfaceData(
            _id=message.get_tlv(LinkTlvs.INTERFACE1_NUMBER.value),
            name=message.get_tlv(LinkTlvs.INTERFACE1_NAME.value),
            mac=message.get_tlv(LinkTlvs.INTERFACE1_MAC.value),
            ip4=message.get_tlv(LinkTlvs.INTERFACE1_IP4.value),
            ip4_mask=message.get_tlv(LinkTlvs.INTERFACE1_IP4_MASK.value),
            ip6=message.get_tlv(LinkTlvs.INTERFACE1_IP6.value),
            ip6_mask=message.get_tlv(LinkTlvs.INTERFACE1_IP6_MASK.value),
        )
        interface_two = InterfaceData(
            _id=message.get_tlv(LinkTlvs.INTERFACE2_NUMBER.value),
            name=message.get_tlv(LinkTlvs.INTERFACE2_NAME.value),
            mac=message.get_tlv(LinkTlvs.INTERFACE2_MAC.value),
            ip4=message.get_tlv(LinkTlvs.INTERFACE2_IP4.value),
            ip4_mask=message.get_tlv(LinkTlvs.INTERFACE2_IP4_MASK.value),
            ip6=message.get_tlv(LinkTlvs.INTERFACE2_IP6.value),
            ip6_mask=message.get_tlv(LinkTlvs.INTERFACE2_IP6_MASK.value),
        )

        link_type = None
        link_type_value = message.get_tlv(LinkTlvs.TYPE.value)
        if link_type_value is not None:
            link_type = LinkTypes(link_type_value)

        link_options = LinkOptions(_type=link_type)
        link_options.delay = message.get_tlv(LinkTlvs.DELAY.value)
        link_options.bandwidth = message.get_tlv(LinkTlvs.BANDWIDTH.value)
        link_options.session = message.get_tlv(LinkTlvs.SESSION.value)
        link_options.per = message.get_tlv(LinkTlvs.PER.value)
        link_options.dup = message.get_tlv(LinkTlvs.DUP.value)
        link_options.jitter = message.get_tlv(LinkTlvs.JITTER.value)
        link_options.mer = message.get_tlv(LinkTlvs.MER.value)
        link_options.burst = message.get_tlv(LinkTlvs.BURST.value)
        link_options.mburst = message.get_tlv(LinkTlvs.MBURST.value)
        link_options.gui_attributes = message.get_tlv(LinkTlvs.GUI_ATTRIBUTES.value)
        link_options.unidirectional = message.get_tlv(LinkTlvs.UNIDIRECTIONAL.value)
        link_options.emulation_id = message.get_tlv(LinkTlvs.EMULATION_ID.value)
        link_options.network_id = message.get_tlv(LinkTlvs.NETWORK_ID.value)
        link_options.key = message.get_tlv(LinkTlvs.KEY.value)
        link_options.opaque = message.get_tlv(LinkTlvs.OPAQUE.value)

        if message.flags & MessageFlags.ADD.value:
            self.session.add_link(node_one_id, node_two_id, interface_one, interface_two, link_options)
        elif message.flags & MessageFlags.DELETE.value:
            self.session.delete_link(node_one_id, node_two_id, interface_one.id, interface_two.id)
        else:
            self.session.update_link(node_one_id, node_two_id, interface_one.id, interface_two.id, link_options)

        return ()

    def handle_execute_message(self, message):
        """
        Execute Message handler

        :param coreapi.CoreExecMessage message: execute message to handle
        :return: reply messages
        """
        node_num = message.get_tlv(ExecuteTlvs.NODE.value)
        execute_num = message.get_tlv(ExecuteTlvs.NUMBER.value)
        execute_time = message.get_tlv(ExecuteTlvs.TIME.value)
        command = message.get_tlv(ExecuteTlvs.COMMAND.value)

        # local flag indicates command executed locally, not on a node
        if node_num is None and not message.flags & MessageFlags.LOCAL.value:
            raise ValueError("Execute Message is missing node number.")

        if execute_num is None:
            raise ValueError("Execute Message is missing execution number.")

        if execute_time is not None:
            self.session.add_event(execute_time, node=node_num, name=None, data=command)
            return ()

        try:
            node = self.session.get_node(node_num)

            # build common TLV items for reply
            tlv_data = b""
            if node_num is not None:
                tlv_data += coreapi.CoreExecuteTlv.pack(ExecuteTlvs.NODE.value, node_num)
            tlv_data += coreapi.CoreExecuteTlv.pack(ExecuteTlvs.NUMBER.value, execute_num)
            tlv_data += coreapi.CoreExecuteTlv.pack(ExecuteTlvs.COMMAND.value, command)

            if message.flags & MessageFlags.TTY.value:
                if node_num is None:
                    raise NotImplementedError
                # echo back exec message with cmd for spawning interactive terminal
                if command == "bash":
                    command = "/bin/bash"
                res = node.termcmdstring(command)
                tlv_data += coreapi.CoreExecuteTlv.pack(ExecuteTlvs.RESULT.value, res)
                reply = coreapi.CoreExecMessage.pack(MessageFlags.TTY.value, tlv_data)
                return reply,
            else:
                logging.info("execute message with cmd=%s", command)
                # execute command and send a response
                if message.flags & MessageFlags.STRING.value or message.flags & MessageFlags.TEXT.value:
                    # shlex.split() handles quotes within the string
                    if message.flags & MessageFlags.LOCAL.value:
                        status, res = utils.cmd_output(command)
                    else:
                        status, res = node.cmd_output(command)
                    logging.info("done exec cmd=%s with status=%d res=(%d bytes)", command, status, len(res))
                    if message.flags & MessageFlags.TEXT.value:
                        tlv_data += coreapi.CoreExecuteTlv.pack(ExecuteTlvs.RESULT.value, res)
                    if message.flags & MessageFlags.STRING.value:
                        tlv_data += coreapi.CoreExecuteTlv.pack(ExecuteTlvs.STATUS.value, status)
                    reply = coreapi.CoreExecMessage.pack(0, tlv_data)
                    return reply,
                # execute the command with no response
                else:
                    if message.flags & MessageFlags.LOCAL.value:
                        utils.mute_detach(command)
                    else:
                        node.cmd(command, wait=False)
        except KeyError:
            logging.exception("error getting object: %s", node_num)
            # XXX wait and queue this message to try again later
            # XXX maybe this should be done differently
            if not message.flags & MessageFlags.LOCAL.value:
                time.sleep(0.125)
                self.queue_message(message)

        return ()

    def handle_register_message(self, message):
        """
        Register Message Handler

        :param coreapi.CoreRegMessage message: register message to handle
        :return: reply messages
        """
        replies = []

        # execute a Python script or XML file
        execute_server = message.get_tlv(RegisterTlvs.EXECUTE_SERVER.value)
        if execute_server:
            try:
                logging.info("executing: %s", execute_server)
                if message.flags & MessageFlags.STRING.value:
                    old_session_ids = set(self.coreemu.sessions.keys())
                sys.argv = shlex.split(execute_server)
                file_name = sys.argv[0]

                if os.path.splitext(file_name)[1].lower() == ".xml":
                    session = self.coreemu.create_session(master=False)
                    try:
                        session.open_xml(file_name, start=True)
                    except:
                        self.coreemu.delete_session(session.id)
                        raise
                else:
                    thread = threading.Thread(
                        target=execfile,
                        args=(file_name, {"__file__": file_name, "coreemu": self.coreemu})
                    )
                    thread.daemon = True
                    thread.start()
                    # allow time for session creation
                    time.sleep(0.25)

                if message.flags & MessageFlags.STRING.value:
                    new_session_ids = set(self.coreemu.sessions.keys())
                    new_sid = new_session_ids.difference(old_session_ids)
                    try:
                        sid = new_sid.pop()
                        logging.info("executed: %s as session %d", execute_server, sid)
                    except KeyError:
                        logging.info("executed %s with unknown session ID", execute_server)
                        return replies

                    logging.debug("checking session %d for RUNTIME state", sid)
                    session = self.coreemu.sessions.get(sid)
                    retries = 10
                    # wait for session to enter RUNTIME state, to prevent GUI from
                    # connecting while nodes are still being instantiated
                    while session.state != EventTypes.RUNTIME_STATE.value:
                        logging.debug("waiting for session %d to enter RUNTIME state", sid)
                        time.sleep(1)
                        retries -= 1
                        if retries <= 0:
                            logging.debug("session %d did not enter RUNTIME state", sid)
                            return replies

                    tlv_data = coreapi.CoreRegisterTlv.pack(RegisterTlvs.EXECUTE_SERVER.value, execute_server)
                    tlv_data += coreapi.CoreRegisterTlv.pack(RegisterTlvs.SESSION.value, "%s" % sid)
                    message = coreapi.CoreRegMessage.pack(0, tlv_data)
                    replies.append(message)
            except Exception as e:
                logging.exception("error executing: %s", execute_server)
                tlv_data = coreapi.CoreExceptionTlv.pack(ExceptionTlvs.LEVEL.value, 2)
                tlv_data += coreapi.CoreExceptionTlv.pack(ExceptionTlvs.TEXT.value, str(e))
                message = coreapi.CoreExceptionMessage.pack(0, tlv_data)
                replies.append(message)

            return replies

        gui = message.get_tlv(RegisterTlvs.GUI.value)
        if gui is None:
            logging.debug("ignoring Register message")
        else:
            # register capabilities with the GUI
            self.master = True

            # find the session containing this client and set the session to master
            for _id in self.coreemu.sessions:
                session = self.coreemu.sessions[_id]
                if self in session.broker.session_clients:
                    logging.debug("setting session to master: %s", session.id)
                    session.master = True
                    break

            replies.append(self.register())
            replies.append(self.session_message())

        return replies

    def handle_config_message(self, message):
        """
        Configuration Message handler

        :param coreapi.CoreConfMessage message: configuration message to handle
        :return: reply messages
        """
        # convert config message to standard config data object
        config_data = ConfigData(
            node=message.get_tlv(ConfigTlvs.NODE.value),
            object=message.get_tlv(ConfigTlvs.OBJECT.value),
            type=message.get_tlv(ConfigTlvs.TYPE.value),
            data_types=message.get_tlv(ConfigTlvs.DATA_TYPES.value),
            data_values=message.get_tlv(ConfigTlvs.VALUES.value),
            captions=message.get_tlv(ConfigTlvs.CAPTIONS.value),
            bitmap=message.get_tlv(ConfigTlvs.BITMAP.value),
            possible_values=message.get_tlv(ConfigTlvs.POSSIBLE_VALUES.value),
            groups=message.get_tlv(ConfigTlvs.GROUPS.value),
            session=message.get_tlv(ConfigTlvs.SESSION.value),
            interface_number=message.get_tlv(ConfigTlvs.INTERFACE_NUMBER.value),
            network_id=message.get_tlv(ConfigTlvs.NETWORK_ID.value),
            opaque=message.get_tlv(ConfigTlvs.OPAQUE.value)
        )
        logging.debug("configuration message for %s node %s", config_data.object, config_data.node)
        message_type = ConfigFlags(config_data.type)

        replies = []

        # handle session configuration
        if config_data.object == "all":
            replies = self.handle_config_all(message_type, config_data)
        elif config_data.object == self.session.options.name:
            replies = self.handle_config_session(message_type, config_data)
        elif config_data.object == self.session.location.name:
            self.handle_config_location(message_type, config_data)
        elif config_data.object == self.session.metadata.name:
            replies = self.handle_config_metadata(message_type, config_data)
        elif config_data.object == self.session.broker.name:
            self.handle_config_broker(message_type, config_data)
        elif config_data.object == self.session.services.name:
            replies = self.handle_config_services(message_type, config_data)
        elif config_data.object == self.session.mobility.name:
            self.handle_config_mobility(message_type, config_data)
        elif config_data.object in self.session.mobility.models:
            replies = self.handle_config_mobility_models(message_type, config_data)
        elif config_data.object == self.session.emane.name:
            replies = self.handle_config_emane(message_type, config_data)
        elif config_data.object in self.session.emane.models:
            replies = self.handle_config_emane_models(message_type, config_data)
        else:
            raise Exception("no handler for configuration: %s", config_data.object)

        for reply in replies:
            self.handle_broadcast_config(reply)

        return []

    def handle_config_all(self, message_type, config_data):
        replies = []

        if message_type == ConfigFlags.RESET:
            node_id = config_data.node
            self.session.location.reset()
            self.session.services.reset()
            self.session.mobility.config_reset(node_id)
            self.session.emane.config_reset(node_id)
        else:
            raise Exception("cant handle config all: %s" % message_type)

        return replies

    def handle_config_session(self, message_type, config_data):
        replies = []
        if message_type == ConfigFlags.REQUEST:
            type_flags = ConfigFlags.NONE.value
            config = self.session.options.get_configs()
            config_response = ConfigShim.config_data(0, None, type_flags, self.session.options, config)
            replies.append(config_response)
        elif message_type != ConfigFlags.RESET and config_data.data_values:
            values = ConfigShim.str_to_dict(config_data.data_values)
            for key in values:
                value = values[key]
                self.session.options.set_config(key, value)
        return replies

    def handle_config_location(self, message_type, config_data):
        if message_type == ConfigFlags.RESET:
            self.session.location.reset()
        else:
            if not config_data.data_values:
                logging.warning("location data missing")
            else:
                values = [float(x) for x in config_data.data_values.split("|")]

                # Cartesian coordinate reference point
                refx, refy = values[0], values[1]
                refz = 0.0
                lat, lon, alt = values[2], values[3], values[4]
                # xyz point
                self.session.location.refxyz = (refx, refy, refz)
                # geographic reference point
                self.session.location.setrefgeo(lat, lon, alt)
                self.session.location.refscale = values[5]
                logging.info("location configured: %s = %s scale=%s", self.session.location.refxyz,
                            self.session.location.refgeo, self.session.location.refscale)
                logging.info("location configured: UTM%s", self.session.location.refutm)

    def handle_config_metadata(self, message_type, config_data):
        replies = []
        if message_type == ConfigFlags.REQUEST:
            node_id = config_data.node
            metadata_configs = self.session.metadata.get_configs()
            data_values = "|".join(["%s=%s" % (x, metadata_configs[x]) for x in metadata_configs])
            data_types = tuple(ConfigDataTypes.STRING.value for _ in self.session.metadata.get_configs())
            config_response = ConfigData(
                message_type=0,
                node=node_id,
                object=self.session.metadata.name,
                type=ConfigFlags.NONE.value,
                data_types=data_types,
                data_values=data_values
            )
            replies.append(config_response)
        elif message_type != ConfigFlags.RESET and config_data.data_values:
            values = ConfigShim.str_to_dict(config_data.data_values)
            for key in values:
                value = values[key]
                self.session.metadata.set_config(key, value)
        return replies

    def handle_config_broker(self, message_type, config_data):
        if message_type not in [ConfigFlags.REQUEST, ConfigFlags.RESET]:
            session_id = config_data.session
            if not config_data.data_values:
                logging.info("emulation server data missing")
            else:
                values = config_data.data_values.split("|")

                # string of "server:ip:port,server:ip:port,..."
                server_strings = values[0]
                server_list = server_strings.split(",")

                for server in server_list:
                    server_items = server.split(":")
                    name, host, port = server_items[:3]

                    if host == "":
                        host = None

                    if port == "":
                        port = None
                    else:
                        port = int(port)

                    if session_id is not None:
                        # receive session ID and my IP from master
                        self.session.broker.session_id_master = int(session_id.split("|")[0])
                        self.session.broker.myip = host
                        host = None
                        port = None

                    # this connects to the server immediately; maybe we should wait
                    # or spin off a new "client" thread here
                    self.session.broker.addserver(name, host, port)
                    self.session.broker.setupserver(name)

    def handle_config_services(self, message_type, config_data):
        replies = []
        node_id = config_data.node
        opaque = config_data.opaque

        if message_type == ConfigFlags.REQUEST:
            session_id = config_data.session
            opaque = config_data.opaque

            logging.debug("configuration request: node(%s) session(%s) opaque(%s)", node_id, session_id, opaque)

            # send back a list of available services
            if opaque is None:
                type_flag = ConfigFlags.NONE.value
                data_types = tuple(repeat(ConfigDataTypes.BOOL.value, len(ServiceManager.services)))

                # sort groups by name and map services to groups
                groups = set()
                group_map = {}
                for name in ServiceManager.services:
                    service_name = ServiceManager.services[name]
                    group = service_name.group
                    groups.add(group)
                    group_map.setdefault(group, []).append(service_name)
                groups = sorted(groups, key=lambda x: x.lower())

                # define tlv values in proper order
                captions = []
                possible_values = []
                values = []
                group_strings = []
                start_index = 1
                logging.info("sorted groups: %s", groups)
                for group in groups:
                    services = sorted(group_map[group], key=lambda x: x.name.lower())
                    logging.info("sorted services for group(%s): %s", group, services)
                    end_index = start_index + len(services) - 1
                    group_strings.append("%s:%s-%s" % (group, start_index, end_index))
                    start_index += len(services)
                    for service_name in services:
                        captions.append(service_name.name)
                        values.append("0")
                        if service_name.custom_needed:
                            possible_values.append("1")
                        else:
                            possible_values.append("")

                # format for tlv
                captions = "|".join(captions)
                possible_values = "|".join(possible_values)
                values = "|".join(values)
                groups = "|".join(group_strings)
            # send back the properties for this service
            else:
                if not node_id:
                    return replies

                node = self.session.get_node(node_id)
                if node is None:
                    logging.warning("request to configure service for unknown node %s", node_id)
                    return replies

                services = ServiceShim.servicesfromopaque(opaque)
                if not services:
                    return replies

                servicesstring = opaque.split(":")
                if len(servicesstring) == 3:
                    # a file request: e.g. "service:zebra:quagga.conf"
                    file_name = servicesstring[2]
                    service_name = services[0]
                    file_data = self.session.services.get_service_file(node, service_name, file_name)
                    self.session.broadcast_file(file_data)
                    # short circuit this request early to avoid returning response below
                    return replies

                # the first service in the list is the one being configured
                service_name = services[0]
                # send back:
                # dirs, configs, startindex, startup, shutdown, metadata, config
                type_flag = ConfigFlags.UPDATE.value
                data_types = tuple(repeat(ConfigDataTypes.STRING.value, len(ServiceShim.keys)))
                service = self.session.services.get_service(node_id, service_name, default_service=True)
                values = ServiceShim.tovaluelist(node, service)
                captions = None
                possible_values = None
                groups = None

            config_response = ConfigData(
                message_type=0,
                node=node_id,
                object=self.session.services.name,
                type=type_flag,
                data_types=data_types,
                data_values=values,
                captions=captions,
                possible_values=possible_values,
                groups=groups,
                session=session_id,
                opaque=opaque
            )
            replies.append(config_response)
        elif message_type == ConfigFlags.RESET:
            self.session.services.reset()
        else:
            data_types = config_data.data_types
            values = config_data.data_values

            error_message = "services config message that I don't know how to handle"
            if values is None:
                logging.error(error_message)
            else:
                if opaque is None:
                    values = values.split("|")
                    # store default services for a node type in self.defaultservices[]
                    if data_types is None or data_types[0] != ConfigDataTypes.STRING.value:
                        logging.info(error_message)
                        return None
                    key = values.pop(0)
                    self.session.services.default_services[key] = values
                    logging.debug("default services for type %s set to %s", key, values)
                elif node_id:
                    services = ServiceShim.servicesfromopaque(opaque)
                    if services:
                        service_name = services[0]

                        # set custom service for node
                        self.session.services.set_service(node_id, service_name)

                        # set custom values for custom service
                        service = self.session.services.get_service(node_id, service_name)
                        if not service:
                            raise ValueError("custom service(%s) for node(%s) does not exist", service_name, node_id)

                        values = ConfigShim.str_to_dict(values)
                        for name in values:
                            value = values[name]
                            ServiceShim.setvalue(service, name, value)

        return replies

    def handle_config_mobility(self, message_type, _):
        if message_type == ConfigFlags.RESET:
            self.session.mobility.reset()

    def handle_config_mobility_models(self, message_type, config_data):
        replies = []
        node_id = config_data.node
        object_name = config_data.object
        interface_id = config_data.interface_number
        values_str = config_data.data_values

        if interface_id is not None:
            node_id = node_id * 1000 + interface_id

        logging.debug("received configure message for %s nodenum: %s", object_name, node_id)
        if message_type == ConfigFlags.REQUEST:
            logging.info("replying to configure request for model: %s", object_name)
            typeflags = ConfigFlags.NONE.value

            model_class = self.session.mobility.models.get(object_name)
            if not model_class:
                logging.warning("model class does not exist: %s", object_name)
                return []

            config = self.session.mobility.get_model_config(node_id, object_name)
            config_response = ConfigShim.config_data(0, node_id, typeflags, model_class, config)
            replies.append(config_response)
        elif message_type != ConfigFlags.RESET:
            # store the configuration values for later use, when the node
            if not object_name:
                logging.warning("no configuration object for node: %s", node_id)
                return []

            parsed_config = {}
            if values_str:
                parsed_config = ConfigShim.str_to_dict(values_str)

            self.session.mobility.set_model_config(node_id, object_name, parsed_config)

        return replies

    def handle_config_emane(self, message_type, config_data):
        replies = []
        node_id = config_data.node
        object_name = config_data.object
        interface_id = config_data.interface_number
        values_str = config_data.data_values

        if interface_id is not None:
            node_id = node_id * 1000 + interface_id

        logging.debug("received configure message for %s nodenum: %s", object_name, node_id)
        if message_type == ConfigFlags.REQUEST:
            logging.info("replying to configure request for %s model", object_name)
            typeflags = ConfigFlags.NONE.value
            config = self.session.emane.get_configs()
            config_response = ConfigShim.config_data(0, node_id, typeflags, self.session.emane.emane_config, config)
            replies.append(config_response)
        elif message_type != ConfigFlags.RESET:
            if not object_name:
                logging.info("no configuration object for node %s", node_id)
                return []

            if values_str:
                config = ConfigShim.str_to_dict(values_str)
                self.session.emane.set_configs(config)

        # extra logic to start slave Emane object after nemid has been configured from the master
        if message_type == ConfigFlags.UPDATE and self.session.master is False:
            # instantiation was previously delayed by setup returning Emane.NOT_READY
            self.session.instantiate()

        return replies

    def handle_config_emane_models(self, message_type, config_data):
        replies = []
        node_id = config_data.node
        object_name = config_data.object
        interface_id = config_data.interface_number
        values_str = config_data.data_values

        if interface_id is not None:
            node_id = node_id * 1000 + interface_id

        logging.debug("received configure message for %s nodenum: %s", object_name, node_id)
        if message_type == ConfigFlags.REQUEST:
            logging.info("replying to configure request for model: %s", object_name)
            typeflags = ConfigFlags.NONE.value

            model_class = self.session.emane.models.get(object_name)
            if not model_class:
                logging.warning("model class does not exist: %s", object_name)
                return []

            config = self.session.emane.get_model_config(node_id, object_name)
            config_response = ConfigShim.config_data(0, node_id, typeflags, model_class, config)
            replies.append(config_response)
        elif message_type != ConfigFlags.RESET:
            # store the configuration values for later use, when the node
            if not object_name:
                logging.warning("no configuration object for node: %s", node_id)
                return []

            parsed_config = {}
            if values_str:
                parsed_config = ConfigShim.str_to_dict(values_str)

            self.session.emane.set_model_config(node_id, object_name, parsed_config)

        return replies

    def handle_file_message(self, message):
        """
        File Message handler

        :param coreapi.CoreFileMessage message: file message to handle
        :return: reply messages
        """
        if message.flags & MessageFlags.ADD.value:
            node_num = message.get_tlv(FileTlvs.NODE.value)
            file_name = message.get_tlv(FileTlvs.NAME.value)
            file_type = message.get_tlv(FileTlvs.TYPE.value)
            source_name = message.get_tlv(FileTlvs.SOURCE_NAME.value)
            data = message.get_tlv(FileTlvs.DATA.value)
            compressed_data = message.get_tlv(FileTlvs.COMPRESSED_DATA.value)

            if compressed_data:
                logging.warning("Compressed file data not implemented for File message.")
                return ()

            if source_name and data:
                logging.warning("ignoring invalid File message: source and data TLVs are both present")
                return ()

            # some File Messages store custom files in services,
            # prior to node creation
            if file_type is not None:
                if file_type.startswith("service:"):
                    _, service_name = file_type.split(':')[:2]
                    self.session.services.set_service_file(node_num, service_name, file_name, data)
                    return ()
                elif file_type.startswith("hook:"):
                    _, state = file_type.split(':')[:2]
                    if not state.isdigit():
                        logging.error("error setting hook having state '%s'", state)
                        return ()
                    state = int(state)
                    self.session.add_hook(state, file_name, source_name, data)
                    return ()

            # writing a file to the host
            if node_num is None:
                if source_name is not None:
                    shutil.copy2(source_name, file_name)
                else:
                    with open(file_name, "w") as open_file:
                        open_file.write(data)
                return ()

            self.session.node_add_file(node_num, source_name, file_name, data)
        else:
            raise NotImplementedError

        return ()

    def handle_interface_message(self, message):
        """
        Interface Message handler.

        :param message: interface message to handle
        :return: reply messages
        """
        logging.info("ignoring Interface message")
        return ()

    def handle_event_message(self, message):
        """
        Event Message handler

        :param coreapi.CoreEventMessage message: event message to handle
        :return: reply messages
        """
        event_data = EventData(
            node=message.get_tlv(EventTlvs.NODE.value),
            event_type=message.get_tlv(EventTlvs.TYPE.value),
            name=message.get_tlv(EventTlvs.NAME.value),
            data=message.get_tlv(EventTlvs.DATA.value),
            time=message.get_tlv(EventTlvs.TIME.value),
            session=message.get_tlv(EventTlvs.SESSION.value)
        )

        if event_data.event_type is None:
            raise NotImplementedError("Event message missing event type")
        event_type = EventTypes(event_data.event_type)
        node_id = event_data.node

        logging.debug("handling event %s at %s", event_type.name, time.ctime())
        if event_type.value <= EventTypes.SHUTDOWN_STATE.value:
            if node_id is not None:
                try:
                    node = self.session.get_node(node_id)
                except KeyError:
                    raise KeyError("Event message for unknown node %d" % node_id)

                # configure mobility models for WLAN added during runtime
                if event_type == EventTypes.INSTANTIATION_STATE and nodeutils.is_node(node, NodeTypes.WIRELESS_LAN):
                    self.session.start_mobility(node_ids=(node.id,))
                    return ()

                logging.warning("dropping unhandled Event message with node number")
                return ()
            self.session.set_state(event_type)

        if event_type == EventTypes.DEFINITION_STATE:
            # clear all session objects in order to receive new definitions
            self.session.clear()
        elif event_type == EventTypes.INSTANTIATION_STATE:
            if len(self.handler_threads) > 1:
                # TODO: sync handler threads here before continuing
                time.sleep(2.0)  # XXX
            # done receiving node/link configuration, ready to instantiate
            self.session.instantiate()

            # after booting nodes attempt to send emulation id for nodes waiting on status
            for _id in self.session.nodes:
                self.send_node_emulation_id(_id)
        elif event_type == EventTypes.RUNTIME_STATE:
            if self.session.master:
                logging.warning("Unexpected event message: RUNTIME state received at session master")
            else:
                # master event queue is started in session.checkruntime()
                self.session.start_events()
        elif event_type == EventTypes.DATACOLLECT_STATE:
            self.session.data_collect()
        elif event_type == EventTypes.SHUTDOWN_STATE:
            if self.session.master:
                logging.warning("Unexpected event message: SHUTDOWN state received at session master")
        elif event_type in {EventTypes.START, EventTypes.STOP, EventTypes.RESTART, EventTypes.PAUSE,
                            EventTypes.RECONFIGURE}:
            handled = False
            name = event_data.name
            if name:
                # TODO: register system for event message handlers,
                # like confobjs
                if name.startswith("service:"):
                    self.handle_service_event(event_data)
                    handled = True
                elif name.startswith("mobility:"):
                    self.session.mobility_event(event_data)
                    handled = True
            if not handled:
                logging.warning("Unhandled event message: event type %s ", event_type.name)
        elif event_type == EventTypes.FILE_OPEN:
            filename = event_data.name
            self.session.open_xml(filename, start=False)
            self.send_objects()
            return ()
        elif event_type == EventTypes.FILE_SAVE:
            filename = event_data.name
            self.session.save_xml(filename)
        elif event_type == EventTypes.SCHEDULED:
            etime = event_data.time
            node = event_data.node
            name = event_data.name
            data = event_data.data
            if etime is None:
                logging.warning("Event message scheduled event missing start time")
                return ()
            if message.flags & MessageFlags.ADD.value:
                self.session.add_event(float(etime), node=node, name=name, data=data)
            else:
                raise NotImplementedError
        else:
            logging.warning("unhandled event message: event type %s", event_type)

        return ()

    def handle_service_event(self, event_data):
        """
        Handle an Event Message used to start, stop, restart, or validate
        a service on a given node.

        :param EventData event_data: event data to handle
        :return: nothing
        """
        event_type = event_data.event_type
        node_id = event_data.node
        name = event_data.name

        try:
            node = self.session.get_node(node_id)
        except KeyError:
            logging.warning("ignoring event for service '%s', unknown node '%s'", name, node_id)
            return

        fail = ""
        unknown = []
        services = ServiceShim.servicesfromopaque(name)
        for service_name in services:
            service = self.session.services.get_service(node_id, service_name, default_service=True)
            if not service:
                unknown.append(service_name)
                continue

            if event_type == EventTypes.STOP.value or event_type == EventTypes.RESTART.value:
                status = self.session.services.stop_service(node, service)
                if status:
                    fail += "Stop %s," % service.name
            if event_type == EventTypes.START.value or event_type == EventTypes.RESTART.value:
                status = self.session.services.startup_service(node, service)
                if status:
                    fail += "Start %s(%s)," % service.name
            if event_type == EventTypes.PAUSE.value:
                status = self.session.services.validate_service(node, service)
                if status:
                    fail += "%s," % service.name
            if event_type == EventTypes.RECONFIGURE.value:
                self.session.services.service_reconfigure(node, service)

        fail_data = ""
        if len(fail) > 0:
            fail_data += "Fail:" + fail
        unknown_data = ""
        num = len(unknown)
        if num > 0:
            for u in unknown:
                unknown_data += u
                if num > 1:
                    unknown_data += ", "
                num -= 1
            logging.warning("Event requested for unknown service(s): %s", unknown_data)
            unknown_data = "Unknown:" + unknown_data

        event_data = EventData(
            node=node_id,
            event_type=event_type,
            name=name,
            data=fail_data + ";" + unknown_data,
            time="%s" % time.time()
        )

        self.session.broadcast_event(event_data)

    def handle_session_message(self, message):
        """
        Session Message handler

        :param coreapi.CoreSessionMessage message: session message to handle
        :return: reply messages
        """
        session_id_str = message.get_tlv(SessionTlvs.NUMBER.value)
        session_ids = coreapi.str_to_list(session_id_str)
        name_str = message.get_tlv(SessionTlvs.NAME.value)
        names = coreapi.str_to_list(name_str)
        file_str = message.get_tlv(SessionTlvs.FILE.value)
        files = coreapi.str_to_list(file_str)
        thumb = message.get_tlv(SessionTlvs.THUMB.value)
        user = message.get_tlv(SessionTlvs.USER.value)
        logging.debug("SESSION message flags=0x%x sessions=%s" % (message.flags, session_id_str))

        if message.flags == 0:
            for index, session_id in enumerate(session_ids):
                session_id = int(session_id)
                if session_id == 0:
                    session = self.session
                else:
                    session = self.coreemu.sessions.get(session_id)

                if session is None:
                    logging.warning("session %s not found", session_id)
                    continue

                logging.info("request to modify to session: %s", session.id)
                if names is not None:
                    session.name = names[index]

                if files is not None:
                    session.file_name = files[index]

                if thumb:
                    session.set_thumbnail(thumb)

                if user:
                    session.set_user(user)
        elif message.flags & MessageFlags.STRING.value and not message.flags & MessageFlags.ADD.value:
            # status request flag: send list of sessions
            return self.session_message(),
        else:
            # handle ADD or DEL flags
            for session_id in session_ids:
                session_id = int(session_id)
                session = self.coreemu.sessions.get(session_id)

                if session is None:
                    logging.info("session %s not found (flags=0x%x)", session_id, message.flags)
                    continue

                if message.flags & MessageFlags.ADD.value:
                    # connect to the first session that exists
                    logging.info("request to connect to session %s", session_id)

                    # remove client from session broker and shutdown if needed
                    self.remove_session_handlers()
                    self.session.broker.session_clients.remove(self)
                    if not self.session.broker.session_clients and not self.session.is_active():
                        self.coreemu.delete_session(self.session.id)

                    # set session to join
                    self.session = session

                    # add client to session broker and set master if needed
                    if self.master:
                        self.session.master = True
                    self.session.broker.session_clients.append(self)

                    # add broadcast handlers
                    logging.info("adding session broadcast handlers")
                    self.add_session_handlers()

                    if user:
                        self.session.set_user(user)

                    if message.flags & MessageFlags.STRING.value:
                        self.send_objects()
                elif message.flags & MessageFlags.DELETE.value:
                    # shut down the specified session(s)
                    logging.info("request to terminate session %s", session_id)
                    self.coreemu.delete_session(session_id)
                else:
                    logging.warning("unhandled session flags for session %s", session_id)

        return ()

    def send_node_emulation_id(self, node_id):
        """
        Node emulation id to send.

        :param int node_id: node id to send
        :return: nothing
        """
        if node_id in self.node_status_request:
            tlv_data = b""
            tlv_data += coreapi.CoreNodeTlv.pack(NodeTlvs.NUMBER.value, node_id)
            tlv_data += coreapi.CoreNodeTlv.pack(NodeTlvs.EMULATION_ID.value, node_id)
            reply = coreapi.CoreNodeMessage.pack(MessageFlags.ADD.value | MessageFlags.LOCAL.value, tlv_data)

            try:
                self.sendall(reply)
            except IOError:
                logging.exception("error sending node emulation id message: %s", node_id)

            del self.node_status_request[node_id]

    def send_objects(self):
        """
        Return API messages that describe the current session.
        """
        # find all nodes and links

        nodes_data = []
        links_data = []
        with self.session._nodes_lock:
            for node_id in self.session.nodes:
                node = self.session.nodes[node_id]
                node_data = node.data(message_type=MessageFlags.ADD.value)
                if node_data:
                    nodes_data.append(node_data)

                node_links = node.all_link_data(flags=MessageFlags.ADD.value)
                for link_data in node_links:
                    links_data.append(link_data)

        # send all nodes first, so that they will exist for any links
        for node_data in nodes_data:
            self.session.broadcast_node(node_data)

        for link_data in links_data:
            self.session.broadcast_link(link_data)

        # send mobility model info
        for node_id in self.session.mobility.nodes():
            mobility_configs = self.session.mobility.get_all_configs(node_id)
            for model_name in mobility_configs:
                config = mobility_configs[model_name]
                model_class = self.session.mobility.models[model_name]
                logging.debug("mobility config: node(%s) class(%s) values(%s)", node_id, model_class, config)
                config_data = ConfigShim.config_data(0, node_id, ConfigFlags.UPDATE.value, model_class, config)
                self.session.broadcast_config(config_data)

        # send emane model info
        for node_id in self.session.emane.nodes():
            emane_configs = self.session.emane.get_all_configs(node_id)
            for model_name in emane_configs:
                config = emane_configs[model_name]
                model_class = self.session.emane.models[model_name]
                logging.debug("emane config: node(%s) class(%s) values(%s)", node_id, model_class, config)
                config_data = ConfigShim.config_data(0, node_id, ConfigFlags.UPDATE.value, model_class, config)
                self.session.broadcast_config(config_data)

        # service customizations
        service_configs = self.session.services.all_configs()
        for node_id, service in service_configs:
            opaque = "service:%s" % service.name
            data_types = tuple(repeat(ConfigDataTypes.STRING.value, len(ServiceShim.keys)))
            node = self.session.get_node(node_id)
            values = ServiceShim.tovaluelist(node, service)
            config_data = ConfigData(
                message_type=0,
                node=node_id,
                object=self.session.services.name,
                type=ConfigFlags.UPDATE.value,
                data_types=data_types,
                data_values=values,
                session=str(self.session.id),
                opaque=opaque
            )
            self.session.broadcast_config(config_data)

            for file_name, config_data in self.session.services.all_files(service):
                file_data = FileData(
                    message_type=MessageFlags.ADD.value,
                    node=node_id,
                    name=str(file_name),
                    type=opaque,
                    data=str(config_data)
                )
                self.session.broadcast_file(file_data)

        # TODO: send location info

        # send hook scripts
        for state in sorted(self.session._hooks.keys()):
            for file_name, config_data in self.session._hooks[state]:
                file_data = FileData(
                    message_type=MessageFlags.ADD.value,
                    name=str(file_name),
                    type="hook:%s" % state,
                    data=str(config_data)
                )
                self.session.broadcast_file(file_data)

        # send session configuration
        session_config = self.session.options.get_configs()
        config_data = ConfigShim.config_data(0, None, ConfigFlags.UPDATE.value, self.session.options, session_config)
        self.session.broadcast_config(config_data)

        # send session metadata
        metadata_configs = self.session.metadata.get_configs()
        if metadata_configs:
            data_values = "|".join(["%s=%s" % (x, metadata_configs[x]) for x in metadata_configs])
            data_types = tuple(ConfigDataTypes.STRING.value for _ in self.session.metadata.get_configs())
            config_data = ConfigData(
                message_type=0,
                object=self.session.metadata.name,
                type=ConfigFlags.NONE.value,
                data_types=data_types,
                data_values=data_values
            )
            self.session.broadcast_config(config_data)

        logging.info("informed GUI about %d nodes and %d links", len(nodes_data), len(links_data))
