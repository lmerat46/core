import logging

from core.config import ConfigurableManager
from core.config import ConfigurableOptions
from core.config import Configuration
from core.emulator.enumerations import ConfigDataTypes
from core.emulator.enumerations import RegisterTlvs
from core.plugins.sdt import Sdt


class SessionConfig(ConfigurableManager, ConfigurableOptions):
    """
    Provides session configuration.
    """
    name = "session"
    options = [
        Configuration(_id="controlnet", _type=ConfigDataTypes.STRING, label="Control Network"),
        Configuration(_id="controlnet0", _type=ConfigDataTypes.STRING, label="Control Network 0"),
        Configuration(_id="controlnet1", _type=ConfigDataTypes.STRING, label="Control Network 1"),
        Configuration(_id="controlnet2", _type=ConfigDataTypes.STRING, label="Control Network 2"),
        Configuration(_id="controlnet3", _type=ConfigDataTypes.STRING, label="Control Network 3"),
        Configuration(_id="controlnet_updown_script", _type=ConfigDataTypes.STRING, label="Control Network Script"),
        Configuration(_id="enablerj45", _type=ConfigDataTypes.BOOL, default="1", options=["On", "Off"],
                      label="Enable RJ45s"),
        Configuration(_id="preservedir", _type=ConfigDataTypes.BOOL, default="0", options=["On", "Off"],
                      label="Preserve session dir"),
        Configuration(_id="enablesdt", _type=ConfigDataTypes.BOOL, default="0", options=["On", "Off"],
                      label="Enable SDT3D output"),
        Configuration(_id="sdturl", _type=ConfigDataTypes.STRING, default=Sdt.DEFAULT_SDT_URL, label="SDT3D URL")
    ]
    config_type = RegisterTlvs.UTILITY

    def __init__(self):
        super(SessionConfig, self).__init__()
        self.set_configs(self.default_values())

    def get_config(self, _id, node_id=ConfigurableManager._default_node,
                   config_type=ConfigurableManager._default_type, default=None):
        logging.warning("getting config: node_id: %s, config_type %s, default %s", node_id, config_type, default)
        value = super(SessionConfig, self).get_config(_id, node_id, config_type, default)
        if value == "":
            value = default
        return value

    def get_config_bool(self, name, default=None):
        value = self.get_config(name)
        if value is None:
            return default
        return value.lower() == "true"

    def get_config_int(self, name, default=None):
        value = self.get_config(name, default=default)
        if value is not None:
            value = int(value)
        return value


class SessionMetaData(ConfigurableManager):
    """
    Metadata is simply stored in a configs[] dict. Key=value pairs are
    passed in from configure messages destined to the "metadata" object.
    The data is not otherwise interpreted or processed.
    """
    name = "metadata"
    config_type = RegisterTlvs.UTILITY
