import logging

from core.emulator.coreemu import CoreEmu
from core.emulator.emudata import IpPrefixes
from core.emulator.enumerations import NodeTypes, EventTypes


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    coreemu = CoreEmu()
    session = coreemu.create_session()
    session.set_state(EventTypes.CONFIGURATION_STATE)

    # create nodes and interfaces
    try:
        prefixes = IpPrefixes(ip4_prefix="10.83.0.0/16")
        switch = session.add_node(_type=NodeTypes.SWITCH)
        node_one = session.add_node(_type=NodeTypes.DOCKER)
        node_two = session.add_node(_type=NodeTypes.DOCKER)
        node_three = session.add_node()
        interface_one = prefixes.create_interface(node_one)
        interface_two = prefixes.create_interface(node_two)
        interface_three = prefixes.create_interface(node_three)

        # add link
        input("press key to continue")
        session.add_link(node_one.id, switch.id, interface_one)
        session.add_link(node_two.id, switch.id, interface_two)
        session.add_link(node_three.id, switch.id, interface_three)
        print(node_one.cmd_output("ifconfig"))
        print(node_two.cmd_output("ifconfig"))
        input("press key to continue")
    finally:
        input("continue to shutdown")
        coreemu.shutdown()