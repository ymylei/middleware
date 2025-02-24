from middlewared.plugins.smb_.registry_base import RegObj, RegistrySchema
from bidict import bidict

LOGLEVEL_MAP = bidict({
    '0': 'NONE',
    '1': 'MINIMUM',
    '2': 'NORMAL',
    '3': 'FULL',
    '10': 'DEBUG',
})


class GlobalSchema(RegistrySchema):
    def convert_schema_to_registry(self, data_in, data_out):
        super().convert_schema_to_registry(data_in, data_out)
        shares = data_in.pop('shares')
        ms_accounts = data_in.pop('ms_accounts')

        """
        When guest access permitted on any share:
        1) enable anonymous IPC$ share access and anonymous access to SAMR
           and LSADCERPC services.
        2) map to guest on bad user.
        """
        guest_enabled = any(filter(lambda x: x['guestok'], shares))

        data_out.update({
            'disable spoolss': {'parsed': True},
            'dns proxy': {'parsed': False},
            'load printers': {'parsed': False},
            'max log size': {'parsed': 5120},
            'printcap name': {'parsed': '/dev/null'},
            'restrict anonymous': {'parsed': 0 if guest_enabled else 2},
        })

        if guest_enabled:
            data_out['map to guest'] = {'parsed': 'Bad User'}

        ds_state = data_in.pop('ds_state')
        if ms_accounts and ds_state['activedirectory'] == 'DISABLED':
            data_out['username map'] = {'parsed': '/etc/smbusername.map'}

        if ds_state['ldap'] in ['LEAVING', 'DISABLED']:
            data_out.update({
                'passdb backend': {'parsed': 'tdbsam:/root/samba/private/passdb.tdb'},
            })
        return

    def smb_proto_transform(entry, conf):
        val = conf.pop(entry.smbconf, entry.default)
        if val == entry.default:
            return val

        return val['raw'] == "NT1"

    def set_min_protocol(entry, val, data_in, data_out):
        data_out[entry.smbconf] = {"parsed": "NT1" if val else "SMB2_10"}
        return

    def log_level_transform(entry, conf):
        conf.pop('logging', None)
        val = conf.pop(entry.smbconf, entry.default)
        if val == entry.default:
            return val

        if val['raw'].startswith("syslog@"):
            val = val['raw'][len("syslog@")]

        return LOGLEVEL_MAP.get(val['raw'].split()[0])

    def set_log_level(entry, val, data_in, data_out):
        loglevelint = int(LOGLEVEL_MAP.inv.get(val, 1))
        loglevel = f"{loglevelint} auth_json_audit:3@/var/log/samba4/auth_audit.log"
        if data_in['syslog']:
            logging = f'syslog@{"3" if loglevelint > 3 else loglevelint} file'
        else:
            logging = "file"
        data_out.update({
            "log level": {"parsed": loglevel},
            "logging": {"parsed": logging},
        })
        return

    def bind_ip_transform(entry, conf):
        val = conf.pop(entry.smbconf, entry.default)
        if val == entry.default:
            return val

        if type(val) == dict:
            bind_ips = val['raw'].split()
        else:
            bind_ips = val

        if bind_ips:
            bind_ips.remove("127.0.0.1")

        return bind_ips

    def set_bind_ips(entry, val, data_in, data_out):
        if val:
            val.insert(0, "127.0.0.1")
            data_out['interfaces'] = {"parsed": val}

        data_out['bind interfaces only'] = {"parsed": True}
        return

    def mask_transform(entry, conf):
        val = conf.pop(entry.smbconf, entry.default)
        if val == entry.default:
            return val

        if val['raw'] == "0775":
            return ""

        return val['raw']

    def set_mask(entry, val, data_in, data_out):
        if not val:
            val = entry.default

        data_out[entry.smbconf] = {"parsed": val}
        return

    schema = [
        RegObj("netbiosname_local", "netbios name", ""),
        RegObj("workgroup", "workgroup", "WORKGROUP"),
        RegObj("netbiosalias", "netbios aliases", []),
        RegObj("description", "server string", ""),
        RegObj("enable_smb1", "server min protocol", False,
               smbconf_parser=smb_proto_transform, schema_parser=set_min_protocol),
        RegObj("unixcharset", "unix charset", "UTF8"),
        RegObj("syslog", "syslog only", False),
        RegObj("localmaster", "local master", False),
        RegObj("loglevel", "log level", "MINIMUM",
               smbconf_parser=log_level_transform, schema_parser=set_log_level),
        RegObj("guest", "guest account", "nobody"),
        RegObj("filemask", "create mask", "0775",
               smbconf_parser=mask_transform, schema_parser=set_mask),
        RegObj("dirmask", "directory mask", "0775",
               smbconf_parser=mask_transform, schema_parser=set_mask),
        RegObj("ntlmv1_auth", "ntlm auth", False),
        RegObj("bindip", "interfaces", [],
               smbconf_parser=bind_ip_transform, schema_parser=set_bind_ips),
    ]

    def __init__(self):
        super().__init__(self.schema)
