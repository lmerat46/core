"""
TODO: probably goes away, or implement the usage of "unshare", or docker formal.
"""

import logging
import socket
import threading
from socket import AF_INET
from socket import AF_INET6

from core import CoreCommandError, utils
from core import constants
from core.nodes.base import CoreNetworkBase
from core.emulator.data import LinkData
from core.emulator.enumerations import LinkTypes
from core.emulator.enumerations import NodeTypes
from core.emulator.enumerations import RegisterTlvs
from core.nodes import ipaddress
from core.nodes.interface import GreTap
from core.nodes.interface import Veth
from core.nodes.network import EbtablesQueue
from core.nodes.network import GreTapBridge

# a global object because all WLANs share the same queue
# cannot have multiple threads invoking the ebtables commnd
ebtables_queue = EbtablesQueue()

ebtables_lock = threading.Lock()

utils.check_executables([
    constants.IP_BIN,
    constants.EBTABLES_BIN,
    constants.TC_BIN
])


def ebtables_commands(call, commands):
    with ebtables_lock:
        for command in commands:
            call(command)


class OvsNet(CoreNetworkBase):
    """
    Used to be LxBrNet.

    Base class for providing Openvswitch functionality to objects that create bridges.
    """

    policy = "DROP"

    def __init__(self, session, _id=None, name=None, start=True, policy=None):
        """
        Creates an OvsNet instance.

        :param core.session.Session session: session this object is a part of
        :param _id:
        :param name:
        :param start:
        :param policy:
        :return:
        """

        CoreNetworkBase.__init__(self, session, _id, name, start)

        if policy:
            self.policy = policy
        else:
            self.policy = self.__class__.policy

        session_id = self.session.short_session_id()
        self.bridge_name = "b.%s.%s" % (str(self.id), session_id)
        self.up = False

        if start:
            self.startup()
            ebtables_queue.startupdateloop(self)

    def startup(self):
        """

        :return:
        :raises CoreCommandError: when there is a command exception
        """
        utils.check_cmd([constants.OVS_BIN, "add-br", self.bridge_name])

        # turn off spanning tree protocol and forwarding delay
        # TODO: appears stp and rstp are off by default, make sure this always holds true
        # TODO: apears ovs only supports rstp forward delay and again it's off by default
        utils.check_cmd([constants.IP_BIN, "link", "set", self.bridge_name, "up"])

        # create a new ebtables chain for this bridge
        ebtables_commands(utils.check_cmd, [
            [constants.EBTABLES_BIN, "-N", self.bridge_name, "-P", self.policy],
            [constants.EBTABLES_BIN, "-A", "FORWARD", "--logical-in", self.bridge_name, "-j", self.bridge_name]
        ])

        self.up = True

    def shutdown(self):
        if not self.up:
            logging.info("exiting shutdown, object is not up")
            return

        ebtables_queue.stopupdateloop(self)

        try:
            utils.check_cmd([constants.IP_BIN, "link", "set", self.bridge_name, "down"])
            utils.check_cmd([constants.OVS_BIN, "del-br", self.bridge_name])
            ebtables_commands(utils.check_cmd, [
                [constants.EBTABLES_BIN, "-D", "FORWARD", "--logical-in", self.bridge_name, "-j", self.bridge_name],
                [constants.EBTABLES_BIN, "-X", self.bridge_name]
            ])
        except CoreCommandError:
            logging.exception("error bringing bridge down and removing it")

        # removes veth pairs used for bridge-to-bridge connections
        for interface in self.netifs():
            interface.shutdown()

        self._netif.clear()
        self._linked.clear()
        del self.session
        self.up = False

    def attach(self, interface):
        if self.up:
            utils.check_cmd([constants.OVS_BIN, "add-port", self.bridge_name, interface.localname])
            utils.check_cmd([constants.IP_BIN, "link", "set", interface.localname, "up"])

        CoreNetworkBase.attach(self, interface)

    def detach(self, interface):
        if self.up:
            utils.check_cmd([constants.OVS_BIN, "del-port", self.bridge_name, interface.localname])

        CoreNetworkBase.detach(self, interface)

    def linked(self, interface_one, interface_two):
        # check if the network interfaces are attached to this network
        if self._netif[interface_one.netifi] != interface_one:
            raise ValueError("inconsistency for interface %s" % interface_one.name)

        if self._netif[interface_two.netifi] != interface_two:
            raise ValueError("inconsistency for interface %s" % interface_two.name)

        try:
            linked = self._linked[interface_one][interface_two]
        except KeyError:
            if self.policy == "ACCEPT":
                linked = True
            elif self.policy == "DROP":
                linked = False
            else:
                raise ValueError("unknown policy: %s" % self.policy)

            self._linked[interface_one][interface_two] = linked

        return linked

    def unlink(self, interface_one, interface_two):
        """
        Unlink two PyCoreNetIfs, resulting in adding or removing ebtables
        filtering rules.
        """
        with self._linked_lock:
            if not self.linked(interface_one, interface_two):
                return

            self._linked[interface_one][interface_two] = False

        ebtables_queue.ebchange(self)

    def link(self, interface_one, interface_two):
        """
        Link two PyCoreNetIfs together, resulting in adding or removing
        ebtables filtering rules.
        """
        with self._linked_lock:
            if self.linked(interface_one, interface_two):
                return

            self._linked[interface_one][interface_two] = True

        ebtables_queue.ebchange(self)

    def linkconfig(self, netif, bw=None, delay=None, loss=None, duplicate=None,
                   jitter=None, netif2=None, devname=None):
        """
        Configure link parameters by applying tc queuing disciplines on the
        interface.
        """
        if not devname:
            devname = netif.localname

        tc = [constants.TC_BIN, "qdisc", "replace", "dev", devname]
        parent = ["root"]

        # attempt to set bandwidth and update as needed if value changed
        bandwidth_changed = netif.setparam("bw", bw)
        if bandwidth_changed:
            # from tc-tbf(8): minimum value for burst is rate / kernel_hz
            if bw > 0:
                if self.up:
                    burst = max(2 * netif.mtu, bw / 1000)
                    limit = 0xffff  # max IP payload
                    tbf = ["tbf", "rate", str(bw), "burst", str(burst), "limit", str(limit)]
                    logging.info("linkconfig: %s" % [tc + parent + ["handle", "1:"] + tbf])
                    utils.check_cmd(tc + parent + ["handle", "1:"] + tbf)
                netif.setparam("has_tbf", True)
            elif netif.getparam("has_tbf") and bw <= 0:
                tcd = [] + tc
                tcd[2] = "delete"

                if self.up:
                    utils.check_cmd(tcd + parent)

                netif.setparam("has_tbf", False)
                # removing the parent removes the child
                netif.setparam("has_netem", False)

        if netif.getparam("has_tbf"):
            parent = ["parent", "1:1"]

        netem = ["netem"]
        delay_changed = netif.setparam("delay", delay)

        if loss is not None:
            loss = float(loss)
        loss_changed = netif.setparam("loss", loss)

        if duplicate is not None:
            duplicate = float(duplicate)
        duplicate_changed = netif.setparam("duplicate", duplicate)
        jitter_changed = netif.setparam("jitter", jitter)

        # if nothing changed return
        if not any([bandwidth_changed, delay_changed, loss_changed, duplicate_changed, jitter_changed]):
            return

        # jitter and delay use the same delay statement
        if delay is not None:
            netem += ["delay", "%sus" % delay]
        else:
            netem += ["delay", "0us"]

        if jitter is not None:
            netem += ["%sus" % jitter, "25%"]

        if loss is not None and loss > 0:
            netem += ["loss", "%s%%" % min(loss, 100)]

        if duplicate is not None and duplicate > 0:
            netem += ["duplicate", "%s%%" % min(duplicate, 100)]

        if delay <= 0 and jitter <= 0 and loss <= 0 and duplicate <= 0:
            # possibly remove netem if it exists and parent queue wasn"t removed
            if not netif.getparam("has_netem"):
                return

            tc[2] = "delete"

            if self.up:
                logging.info("linkconfig: %s" % ([tc + parent + ["handle", "10:"]],))
                utils.check_cmd(tc + parent + ["handle", "10:"])
            netif.setparam("has_netem", False)
        elif len(netem) > 1:
            if self.up:
                logging.info("linkconfig: %s" % ([tc + parent + ["handle", "10:"] + netem],))
                utils.check_cmd(tc + parent + ["handle", "10:"] + netem)
            netif.setparam("has_netem", True)

    def linknet(self, network):
        """
        Link this bridge with another by creating a veth pair and installing
        each device into each bridge.
        """
        session_id = self.session.short_session_id()

        try:
            _id = "%x" % self.id
        except TypeError:
            _id = "%s" % self.id

        try:
            network_id = "%x" % network.id
        except TypeError:
            network_id = "%s" % network.id

        localname = "veth%s.%s.%s" % (_id, network_id, session_id)

        if len(localname) >= 16:
            raise ValueError("interface local name %s too long" % localname)

        name = "veth%s.%s.%s" % (network_id, _id, session_id)
        if len(name) >= 16:
            raise ValueError("interface name %s too long" % name)

        interface = Veth(node=None, name=name, localname=localname, mtu=1500, net=self, start=self.up)
        self.attach(interface)
        if network.up:
            # this is similar to net.attach() but uses netif.name instead
            # of localname
            utils.check_cmd([constants.OVS_BIN, "add-port", network.bridge_name, interface.name])
            utils.check_cmd([constants.IP_BIN, "link", "set", interface.name, "up"])

        # TODO: is there a native method for this? see if this causes issues
        # i = network.newifindex()
        # network._netif[i] = interface
        # with network._linked_lock:
        #     network._linked[interface] = {}
        # this method call is equal to the above, with a interface.netifi = call
        network.attach(interface)

        interface.net = self
        interface.othernet = network
        return interface

    def getlinknetif(self, network):
        """
        Return the interface of that links this net with another net
        (that were linked using linknet()).
        """
        for interface in self.netifs():
            if hasattr(interface, "othernet") and interface.othernet == network:
                return interface

        return None

    def addrconfig(self, addresses):
        """
        Set addresses on the bridge.
        """
        if not self.up:
            return

        for address in addresses:
            utils.check_cmd([constants.IP_BIN, "addr", "add", str(address), "dev", self.bridge_name])


class OvsCtrlNet(OvsNet):
    policy = "ACCEPT"
    CTRLIF_IDX_BASE = 99  # base control interface index
    DEFAULT_PREFIX_LIST = [
        "172.16.0.0/24 172.16.1.0/24 172.16.2.0/24 172.16.3.0/24 172.16.4.0/24",
        "172.17.0.0/24 172.17.1.0/24 172.17.2.0/24 172.17.3.0/24 172.17.4.0/24",
        "172.18.0.0/24 172.18.1.0/24 172.18.2.0/24 172.18.3.0/24 172.18.4.0/24",
        "172.19.0.0/24 172.19.1.0/24 172.19.2.0/24 172.19.3.0/24 172.19.4.0/24"
    ]

    def __init__(self, session, _id="ctrlnet", name=None, prefix=None, hostid=None,
                 start=True, assign_address=True, updown_script=None, serverintf=None):
        self.prefix = ipaddress.Ipv4Prefix(prefix)
        self.hostid = hostid
        self.assign_address = assign_address
        self.updown_script = updown_script
        self.serverintf = serverintf
        OvsNet.__init__(self, session, _id=_id, name=name, start=start)

    def startup(self):
        if self.detectoldbridge():
            return

        OvsNet.startup(self)
        if self.hostid:
            addr = self.prefix.addr(self.hostid)
        else:
            addr = self.prefix.max_addr()

        message = "Added control network bridge: %s %s" % (self.bridge_name, self.prefix)
        addresses = ["%s/%s" % (addr, self.prefix.prefixlen)]
        if self.assign_address:
            self.addrconfig(addresses=addresses)
            message += " address %s" % addr
        logging.info(message)

        if self.updown_script:
            logging.info("interface %s updown script %s startup called" % (self.bridge_name, self.updown_script))
            utils.check_cmd([self.updown_script, self.bridge_name, "startup"])

        if self.serverintf:
            utils.check_cmd([constants.OVS_BIN, "add-port", self.bridge_name, self.serverintf])
            utils.check_cmd([constants.IP_BIN, "link", "set", self.serverintf, "up"])

    def detectoldbridge(self):
        """
        Occasionally, control net bridges from previously closed sessions are not cleaned up.
        Check if there are old control net bridges and delete them
        """

        output = utils.check_cmd([constants.OVS_BIN, "list-br"])
        output = output.strip()
        if output:
            for line in output.split("\n"):
                bride_name = line.split(".")
                if bride_name[0] == "b" and bride_name[1] == self.id:
                    logging.error("older session may still be running with conflicting id for bridge: %s", line)
                    return True

        return False

    def shutdown(self):
        if self.serverintf:
            try:
                utils.check_cmd([constants.OVS_BIN, "del-port", self.bridge_name, self.serverintf])
            except CoreCommandError:
                logging.exception("error deleting server interface %s to controlnet bridge %s",
                                 self.serverintf, self.bridge_name)

        if self.updown_script:
            try:
                logging.info("interface %s updown script (%s shutdown) called", self.bridge_name, self.updown_script)
                utils.check_cmd([self.updown_script, self.bridge_name, "shutdown"])
            except CoreCommandError:
                logging.exception("error during updown script shutdown")

        OvsNet.shutdown(self)

    def all_link_data(self, flags):
        """
        Do not include CtrlNet in link messages describing this session.
        """
        return []


class OvsPtpNet(OvsNet):
    policy = "ACCEPT"

    def attach(self, interface):
        if len(self._netif) >= 2:
            raise ValueError("point-to-point links support at most 2 network interfaces")
        OvsNet.attach(self, interface)

    def data(self, message_type, lat=None, lon=None, alt=None):
        """
        Do not generate a Node Message for point-to-point links. They are
        built using a link message instead.
        """
        return None

    def all_link_data(self, flags):
        """
        Build CORE API TLVs for a point-to-point link. One Link message describes this network.
        """

        all_links = []

        if len(self._netif) != 2:
            return all_links

        if1, if2 = self._netif.values()

        unidirectional = 0
        if if1.getparams() != if2.getparams():
            unidirectional = 1

        interface1_ip4 = None
        interface1_ip4_mask = None
        interface1_ip6 = None
        interface1_ip6_mask = None
        for address in if1.addrlist:
            ip, _sep, mask = address.partition("/")
            mask = int(mask)
            if ipaddress.is_ipv4_address(ip):
                family = AF_INET
                ipl = socket.inet_pton(family, ip)
                interface1_ip4 = ipaddress.IpAddress(af=family, address=ipl)
                interface1_ip4_mask = mask
            else:
                family = AF_INET6
                ipl = socket.inet_pton(family, ip)
                interface1_ip6 = ipaddress.IpAddress(af=family, address=ipl)
                interface1_ip6_mask = mask

        interface2_ip4 = None
        interface2_ip4_mask = None
        interface2_ip6 = None
        interface2_ip6_mask = None
        for address in if2.addrlist:
            ip, _sep, mask = address.partition("/")
            mask = int(mask)
            if ipaddress.is_ipv4_address(ip):
                family = AF_INET
                ipl = socket.inet_pton(family, ip)
                interface2_ip4 = ipaddress.IpAddress(af=family, address=ipl)
                interface2_ip4_mask = mask
            else:
                family = AF_INET6
                ipl = socket.inet_pton(family, ip)
                interface2_ip6 = ipaddress.IpAddress(af=family, address=ipl)
                interface2_ip6_mask = mask

        # TODO: not currently used
        # loss=netif.getparam("loss")
        link_data = LinkData(
            message_type=flags,
            node1_id=if1.node.id,
            node2_id=if2.node.id,
            link_type=self.linktype,
            unidirectional=unidirectional,
            delay=if1.getparam("delay"),
            bandwidth=if1.getparam("bw"),
            dup=if1.getparam("duplicate"),
            jitter=if1.getparam("jitter"),
            interface1_id=if1.node.getifindex(if1),
            interface1_mac=if1.hwaddr,
            interface1_ip4=interface1_ip4,
            interface1_ip4_mask=interface1_ip4_mask,
            interface1_ip6=interface1_ip6,
            interface1_ip6_mask=interface1_ip6_mask,
            interface2_id=if2.node.getifindex(if2),
            interface2_mac=if2.hwaddr,
            interface2_ip4=interface2_ip4,
            interface2_ip4_mask=interface2_ip4_mask,
            interface2_ip6=interface2_ip6,
            interface2_ip6_mask=interface2_ip6_mask,
        )

        all_links.append(link_data)

        # build a 2nd link message for the upstream link parameters
        # (swap if1 and if2)
        if unidirectional:
            link_data = LinkData(
                message_type=0,
                node1_id=if2.node.id,
                node2_id=if1.node.id,
                delay=if1.getparam("delay"),
                bandwidth=if1.getparam("bw"),
                dup=if1.getparam("duplicate"),
                jitter=if1.getparam("jitter"),
                unidirectional=1,
                interface1_id=if2.node.getifindex(if2),
                interface2_id=if1.node.getifindex(if1)
            )
            all_links.append(link_data)

        return all_links


class OvsSwitchNode(OvsNet):
    apitype = NodeTypes.SWITCH
    policy = "ACCEPT"
    type = "lanswitch"


class OvsHubNode(OvsNet):
    apitype = NodeTypes.HUB
    policy = "ACCEPT"
    type = "hub"

    def __init__(self, session, _id=None, name=None, start=True):
        """
        the Hub node forwards packets to all bridge ports by turning off
        the MAC address learning
        """
        OvsNet.__init__(self, session, _id, name, start)

        if start:
            # TODO: verify that the below flow accomplishes what is desired for a "HUB"
            # TODO: replace "brctl setageing 0"
            utils.check_cmd([constants.OVS_FLOW_BIN, "add-flow", self.bridge_name, "action=flood"])


class OvsWlanNode(OvsNet):
    apitype = NodeTypes.WIRELESS_LAN
    linktype = LinkTypes.WIRELESS
    policy = "DROP"
    type = "wlan"

    def __init__(self, session, _id=None, name=None, start=True, policy=None):
        OvsNet.__init__(self, session, _id, name, start, policy)

        # wireless model such as basic range
        self.model = None
        # mobility model such as scripted
        self.mobility = None

    def attach(self, interface):
        OvsNet.attach(self, interface)

        if self.model:
            interface.poshook = self.model.position_callback

            if interface.node is None:
                return

            x, y, z = interface.node.position.get()
            # invokes any netif.poshook
            interface.setposition(x, y, z)
            # self.model.setlinkparams()

    def setmodel(self, model, config=None):
        """
        Mobility and wireless model.
        """
        logging.info("adding model %s", model.name)

        if model.type == RegisterTlvs.WIRELESS:
            self.model = model(session=self.session, _id=self.id, config=config)
            if self.model.position_callback:
                for interface in self.netifs():
                    interface.poshook = self.model.position_callback
                    if interface.node is not None:
                        x, y, z = interface.node.position.get()
                        interface.poshook(interface, x, y, z)
            self.model.setlinkparams()
        elif model.type == RegisterTlvs.MOBILITY:
            self.mobility = model(session=self.session, _id=self.id, config=config)

    def updatemodel(self, config):
        if not self.model:
            raise ValueError("no model set to update for node(%s)", self.id)
        logging.info("node(%s) updating model(%s): %s", self.id, self.model.name, config)
        self.model.set_configs(config, node_id=self.id)
        if self.model.position_callback:
            for netif in self.netifs():
                netif.poshook = self.model.position_callback
                if netif.node is not None:
                    x, y, z = netif.node.position.get()
                    netif.poshook(netif, x, y, z)
        self.model.updateconfig()

    def all_link_data(self, flags):
        all_links = OvsNet.all_link_data(self, flags)

        if self.model:
            all_links.extend(self.model.all_link_data(flags))

        return all_links


class OvsTunnelNode(GreTapBridge):
    apitype = NodeTypes.TUNNEL
    policy = "ACCEPT"
    type = "tunnel"


class OvsGreTapBridge(OvsNet):
    """
    A network consisting of a bridge with a gretap device for tunneling to
    another system.
    """

    def __init__(self, session, remoteip=None, _id=None, name=None, policy="ACCEPT",
                 localip=None, ttl=255, key=None, start=True):
        OvsNet.__init__(self, session=session, _id=_id, name=name, policy=policy, start=False)
        self.grekey = key
        if self.grekey is None:
            self.grekey = self.session.id ^ self.id

        self.localnum = None
        self.remotenum = None
        self.remoteip = remoteip
        self.localip = localip
        self.ttl = ttl

        if remoteip is None:
            self.gretap = None
        else:
            self.gretap = GreTap(node=self, session=session, remoteip=remoteip,
                                 localip=localip, ttl=ttl, key=self.grekey)
        if start:
            self.startup()

    def startup(self):
        """
        Creates a bridge and adds the gretap device to it.
        """
        OvsNet.startup(self)

        if self.gretap:
            self.attach(self.gretap)

    def shutdown(self):
        """
        Detach the gretap device and remove the bridge.
        """
        if self.gretap:
            self.detach(self.gretap)
            self.gretap.shutdown()
            self.gretap = None

        OvsNet.shutdown(self)

    def addrconfig(self, addresses):
        """
        Set the remote tunnel endpoint. This is a one-time method for
        creating the GreTap device, which requires the remoteip at startup.
        The 1st address in the provided list is remoteip, 2nd optionally
        specifies localip.
        """
        if self.gretap:
            raise ValueError("gretap already exists for %s" % self.name)

        remoteip = addresses[0].split("/")[0]
        localip = None

        if len(addresses) > 1:
            localip = addresses[1].split("/")[0]

        self.gretap = GreTap(session=self.session, remoteip=remoteip,
                             localip=localip, ttl=self.ttl, key=self.grekey)
        self.attach(self.gretap)

    def setkey(self, key):
        """
        Set the GRE key used for the GreTap device. This needs to be set
        prior to instantiating the GreTap device (before addrconfig).
        """
        self.grekey = key


OVS_NODES = {
    NodeTypes.SWITCH: OvsSwitchNode,
    NodeTypes.HUB: OvsHubNode,
    NodeTypes.WIRELESS_LAN: OvsWlanNode,
    NodeTypes.TUNNEL: OvsTunnelNode,
    NodeTypes.TAP_BRIDGE: OvsGreTapBridge,
    NodeTypes.PEER_TO_PEER: OvsPtpNet,
    NodeTypes.CONTROL_NET: OvsCtrlNet
}
