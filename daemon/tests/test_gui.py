"""
Unit tests for testing with a CORE switch.
"""

import threading

from core.api.tlv import coreapi, dataconversion
from core.api.tlv.coreapi import CoreExecuteTlv
from core.emulator.enumerations import CORE_API_PORT, NodeTypes
from core.emulator.enumerations import EventTlvs
from core.emulator.enumerations import EventTypes
from core.emulator.enumerations import ExecuteTlvs
from core.emulator.enumerations import LinkTlvs
from core.emulator.enumerations import LinkTypes
from core.emulator.enumerations import MessageFlags
from core.emulator.enumerations import MessageTypes
from core.nodes import ipaddress


def command_message(node, command):
    """
    Create an execute command TLV message.

    :param node: node to execute command for
    :param command: command to execute
    :return: packed execute message
    """
    tlv_data = CoreExecuteTlv.pack(ExecuteTlvs.NODE, node.id)
    tlv_data += CoreExecuteTlv.pack(ExecuteTlvs.NUMBER, 1)
    tlv_data += CoreExecuteTlv.pack(ExecuteTlvs.COMMAND, command)
    return coreapi.CoreExecMessage.pack(MessageFlags.STRING | MessageFlags.TEXT, tlv_data)


def state_message(state):
    """
    Create a event TLV message for a new state.

    :param core.enumerations.EventTypes state: state to create message for
    :return: packed event message
    """
    tlv_data = coreapi.CoreEventTlv.pack(EventTlvs.TYPE, state.value)
    return coreapi.CoreEventMessage.pack(MessageFlags.NONE, tlv_data)


def switch_link_message(switch, node, address, prefix_len):
    """
    Create a link TLV message for node to a switch, with the provided address and prefix length.

    :param switch: switch for link
    :param node: node for link
    :param address: address node on link
    :param prefix_len: prefix length of address
    :return: packed link message
    """
    tlv_data = coreapi.CoreLinkTlv.pack(LinkTlvs.N1_NUMBER, switch.id)
    tlv_data += coreapi.CoreLinkTlv.pack(LinkTlvs.N2_NUMBER, node.id)
    tlv_data += coreapi.CoreLinkTlv.pack(LinkTlvs.TYPE, LinkTypes.WIRED.value)
    tlv_data += coreapi.CoreLinkTlv.pack(LinkTlvs.INTERFACE2_NUMBER, 0)
    tlv_data += coreapi.CoreLinkTlv.pack(LinkTlvs.INTERFACE2_IP4, address)
    tlv_data += coreapi.CoreLinkTlv.pack(LinkTlvs.INTERFACE2_IP4_MASK, prefix_len)
    return coreapi.CoreLinkMessage.pack(MessageFlags.ADD, tlv_data)


def run_cmd(node, exec_cmd):
    """
    Convenience method for sending commands to a node using the legacy API.

    :param node: The node the command should be issued too
    :param exec_cmd: A string with the command to be run
    :return: Returns the result of the command
    """
    # Set up the command api message
    # tlv_data = CoreExecuteTlv.pack(ExecuteTlvs.NODE.value, node.id)
    # tlv_data += CoreExecuteTlv.pack(ExecuteTlvs.NUMBER.value, 1)
    # tlv_data += CoreExecuteTlv.pack(ExecuteTlvs.COMMAND.value, exec_cmd)
    # message = coreapi.CoreExecMessage.pack(MessageFlags.STRING | MessageFlags.TEXT, tlv_data)
    message = command_message(node, exec_cmd)
    node.session.broker.handlerawmsg(message)

    # Now wait for the response
    server = node.session.broker.servers["localhost"]
    server.sock.settimeout(50.0)

    # receive messages until we get our execute response
    result = None
    status = False
    while True:
        message_header = server.sock.recv(coreapi.CoreMessage.header_len)
        message_type, message_flags, message_length = coreapi.CoreMessage.unpack_header(message_header)
        message_data = server.sock.recv(message_length)

        # If we get the right response return the results
        print("received response message: %s" % message_type)
        if message_type == MessageTypes.EXECUTE:
            message = coreapi.CoreExecMessage(message_flags, message_header, message_data)
            result = message.get_tlv(ExecuteTlvs.RESULT)
            status = message.get_tlv(ExecuteTlvs.STATUS)
            break

    return result, status


class TestGui:
    def test_broker(self, cored):
        """
        Test session broker creation.

        :param core.emulator.coreemu.EmuSession session: session for test
        :param cored: cored daemon server to test with
        """

        # set core daemon to run in the background
        thread = threading.Thread(target=cored.server.serve_forever)
        thread.daemon = True
        thread.start()

        # ip prefix for nodes
        prefix = ipaddress.Ipv4Prefix("10.83.0.0/16")
        daemon = "localhost"

        # add server
        session = cored.server.coreemu.create_session()
        session.broker.addserver(daemon, "127.0.0.1", CORE_API_PORT)

        # setup server
        session.broker.setupserver(daemon)

        # do not want the recvloop running as we will deal ourselves
        session.broker.dorecvloop = False

        # have broker handle a configuration state change
        session.set_state(EventTypes.CONFIGURATION_STATE)
        event_message = state_message(EventTypes.CONFIGURATION_STATE)
        session.broker.handlerawmsg(event_message)

        # create a switch node
        switch = session.add_node(_type=NodeTypes.SWITCH)
        switch.setposition(x=80, y=50)
        switch.server = daemon

        # retrieve switch data representation, create a switch message for broker to handle
        switch_data = switch.data(MessageFlags.ADD)
        switch_message = dataconversion.convert_node(switch_data)
        session.broker.handlerawmsg(switch_message)

        # create node one
        node_one = session.add_node()
        node_one.server = daemon

        # create node two
        node_two = session.add_node()
        node_two.server = daemon

        # create node messages for the broker to handle
        for node in [node_one, node_two]:
            node_data = node.data(MessageFlags.ADD)
            node_message = dataconversion.convert_node(node_data)
            session.broker.handlerawmsg(node_message)

        # create links to switch from nodes for broker to handle
        for index, node in enumerate([node_one, node_two], start=1):
            ip4_address = prefix.addr(index)
            link_message = switch_link_message(switch, node, ip4_address, prefix.prefixlen)
            session.broker.handlerawmsg(link_message)

        # change session to instantiation state
        event_message = state_message(EventTypes.INSTANTIATION_STATE)
        session.broker.handlerawmsg(event_message)

        # Get the ip or last node and ping it from the first
        output, status = run_cmd(node_one, "ip -4 -o addr show dev eth0")
        pingip = output.split()[3].split("/")[0]
        output, status = run_cmd(node_two, "ping -c 5 " + pingip)
        assert not status
