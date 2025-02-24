import datetime
import enum
import errno
import grp
import json
import ntplib
import os
import pwd
import shutil
import socket
import subprocess
import threading
import tdb
import time
import contextlib

from dns import resolver
from middlewared.plugins.smb import SMBCmd, SMBPath, WBCErr
from middlewared.schema import accepts, Bool, Dict, Int, List, Str, Ref
from middlewared.service import job, private, TDBWrapConfigService, Service, ValidationError, ValidationErrors
from middlewared.service_exception import CallError, MatchNotFound
import middlewared.sqlalchemy as sa
from middlewared.utils import run
from middlewared.plugins.directoryservices import DSStatus
from middlewared.plugins.idmap import DSType
import middlewared.utils.osc as osc

AD_SMBCONF_PARAMS = {
    "server role": "member server",
    "kerberos method": "secrets and keytab",
    "security": "ADS",
    "local master": False,
    "domain master": False,
    "preferred master": False,
    "winbind cache time": 7200,
    "winbind max domain connections": 10,
    "client ldap sasl wrapping": "seal",
    "template shell": "/bin/sh",
    "template homedir": None,
    "ads dns update": None,
    "realm": None,
    "allow trusted domains": None,
    "winbind enum users": None,
    "winbind enum groups": None,
    "winbind use default domain": None,
    "winbind nss info": None,
}


class neterr(enum.Enum):
    JOINED = 1
    NOTJOINED = 2
    FAULT = 3

    def to_status(errstr):
        errors_to_rejoin = [
            '0xfffffff6',
            'The name provided is not a properly formed account name',
            'The attempted logon is invalid.'
        ]
        for err in errors_to_rejoin:
            if err in errstr:
                return neterr.NOTJOINED

        return neterr.FAULT


class SRV(enum.Enum):
    DOMAINCONTROLLER = '_ldap._tcp.dc._msdcs.'
    FORESTGLOBALCATALOG = '_ldap._tcp.gc._msdcs.'
    GLOBALCATALOG = '_gc._tcp.'
    KERBEROS = '_kerberos._tcp.'
    KERBEROSDOMAINCONTROLLER = '_kerberos._tcp.dc._msdcs.'
    KPASSWD = '_kpasswd._tcp.'
    LDAP = '_ldap._tcp.'
    PDC = '_ldap._tcp.pdc._msdcs.'


class ActiveDirectory_DNS(object):
    def __init__(self, **kwargs):
        super(ActiveDirectory_DNS, self).__init__()
        self.ad = kwargs.get('conf')
        self.logger = kwargs.get('logger')
        return

    def _get_SRV_records(self, host, dns_timeout):
        """
        Set resolver timeout to 1/3 of the lifetime. The timeout defines
        how long to wait before moving on to the next nameserver in resolv.conf
        """
        srv_records = []

        if not host:
            return srv_records

        r = resolver.Resolver()
        r.lifetime = dns_timeout
        r.timeout = r.lifetime / 3

        try:

            answers = r.query(host, 'SRV')
            srv_records = sorted(
                answers,
                key=lambda a: (int(a.priority), int(a.weight))
            )

        except Exception:
            srv_records = []

        return srv_records

    def port_is_listening(self, host, port, timeout=1):
        ret = False

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout:
            s.settimeout(timeout)

        try:
            s.connect((host, port))
            ret = True

        except Exception as e:
            self.logger.debug("connection to %s failed with error: %s",
                              host, e)
            ret = False

        finally:
            s.close()

        return ret

    def _get_servers(self, srv_prefix):
        """
        We will first try fo find servers based on our AD site. If we don't find
        a server in our site, then we populate list for whole domain. Ticket #27584
        Domain Controllers, Forest Global Catalog Servers, and Kerberos Domain Controllers
        need the site information placed before the 'msdcs' component of the host entry.t
        """
        servers = []
        if not self.ad['domainname']:
            return servers

        if self.ad['site'] and self.ad['site'] != 'Default-First-Site-Name':
            if 'msdcs' in srv_prefix.value:
                parts = srv_prefix.value.split('.')
                srv = '.'.join([parts[0], parts[1]])
                msdcs = '.'.join([parts[2], parts[3]])
                host = f"{srv}.{self.ad['site']}._sites.{msdcs}.{self.ad['domainname']}"
            else:
                host = f"{srv_prefix.value}{self.ad['site']}._sites.{self.ad['domainname']}"
        else:
            host = f"{srv_prefix.value}{self.ad['domainname']}"

        servers = self._get_SRV_records(host, self.ad['dns_timeout'])

        if not servers and self.ad['site']:
            host = f"{srv_prefix.value}{self.ad['domainname']}"
            servers = self._get_SRV_records(host, self.ad['dns_timeout'])

        return servers

    def get_n_working_servers(self, srv=SRV['DOMAINCONTROLLER'], number=1):
        """
        :get_n_working_servers: often only a few working servers are needed and not the whole
        list available on the domain. This takes the SRV record type and number of servers to get
        as arguments.
        """
        servers = self._get_servers(srv)
        found_servers = []
        for server in servers:
            if len(found_servers) == number:
                break

            host = server.target.to_text(True)
            port = int(server.port)
            if self.port_is_listening(host, port, timeout=self.ad['timeout']):
                server_info = {'host': host, 'port': port}
                found_servers.append(server_info)

        if self.ad['verbose_logging']:
            self.logger.debug(f'Request for [{number}] of server type [{srv.name}] returned: {found_servers}')
        return found_servers


class ActiveDirectoryModel(sa.Model):
    __tablename__ = 'directoryservice_activedirectory'

    id = sa.Column(sa.Integer(), primary_key=True)
    ad_domainname = sa.Column(sa.String(120))
    ad_bindname = sa.Column(sa.String(120))
    ad_bindpw = sa.Column(sa.EncryptedText())
    ad_verbose_logging = sa.Column(sa.Boolean())
    ad_allow_trusted_doms = sa.Column(sa.Boolean())
    ad_use_default_domain = sa.Column(sa.Boolean())
    ad_allow_dns_updates = sa.Column(sa.Boolean())
    ad_disable_freenas_cache = sa.Column(sa.Boolean())
    ad_restrict_pam = sa.Column(sa.Boolean())
    ad_site = sa.Column(sa.String(120), nullable=True)
    ad_timeout = sa.Column(sa.Integer())
    ad_dns_timeout = sa.Column(sa.Integer())
    ad_nss_info = sa.Column(sa.String(120), nullable=True)
    ad_enable = sa.Column(sa.Boolean())
    ad_kerberos_realm_id = sa.Column(sa.ForeignKey('directoryservice_kerberosrealm.id', ondelete='SET NULL'),
                                     index=True, nullable=True)
    ad_kerberos_principal = sa.Column(sa.String(255))
    ad_createcomputer = sa.Column(sa.String(255))


class ActiveDirectoryService(TDBWrapConfigService):
    tdb_defaults = {
        "id": 1,
        "domainname": "",
        "bindname": "",
        "bindpw": "",
        "verbose_logging": False,
        "allow_trusted_doms": False,
        "use_default_domain": False,
        "allow_dns_updates": True,
        "kerberos_principal": "",
        "kerberos_realm": None,
        "createcomputer": "",
        "site": "",
        "timeout": 60,
        "dns_timeout": 10,
        "nss_info": None,
        "disable_freenas_cache": False,
        "restrict_pam": False,
        "enable": False,
    }

    class Config:
        service = "activedirectory"
        datastore = 'directoryservice.activedirectory'
        datastore_extend = "activedirectory.ad_extend"
        datastore_prefix = "ad_"
        cli_namespace = "directory_service.activedirectory"

    @private
    async def convert_schema_to_registry(self, data_in, data_out):
        """
        Convert middleware schema SMB shares to an SMB service definition
        """
        params = AD_SMBCONF_PARAMS.copy()

        if not data_in['enable']:
            return

        for k, v in params.items():
            if v is None:
                continue

            data_out[k] = {"raw": str(v), "parsed": v}

        data_out.update({
            "ads dns update": {"parsed": data_in["allow_dns_updates"]},
            "realm": {"parsed": data_in["domainname"].upper()},
            "allow trusted domains": {"parsed": data_in["allow_trusted_doms"]},
            "winbind enum users": {"parsed": not data_in["disable_freenas_cache"]},
            "winbind enum groups": {"parsed": not data_in["disable_freenas_cache"]},
            "winbind use default domain": {"parsed": data_in["use_default_domain"]},
        })

        if data_in.get("nss_info"):
            data_out["winbind nss info"] = {"parsed": data_in["nss_info"]}

        try:
            home_share = await self.middleware.call('sharing.smb.reg_showshare', 'homes')
            home_path = home_share['path']['raw']
        except MatchNotFound:
            home_path = 'home'

        data_out['template homedir'] = {"parsed": f'{home_path}/%D/%U'}

        return

    @private
    async def ad_extend(self, ad):
        smb = await self.middleware.call('smb.config')
        smb_ha_mode = await self.middleware.call('smb.get_smb_ha_mode')
        if smb_ha_mode in ['STANDALONE', 'CLUSTERED']:
            ad.update({
                'netbiosname': smb['netbiosname_local'],
                'netbiosalias': smb['netbiosalias']
            })
        elif smb_ha_mode == 'UNIFIED':
            ngc = await self.middleware.call('network.configuration.config')
            ad.update({
                'netbiosname': ngc['hostname_virtual'],
                'netbiosalias': smb['netbiosalias']
            })
        elif smb_ha_mode == 'LEGACY':
            ngc = await self.middleware.call('network.configuration.config')
            ad.update({
                'netbiosname': ngc['hostname'],
                'netbiosname_b': ngc['hostname_b'],
                'netbiosalias': smb['netbiosalias']
            })

        if ad.get('nss_info'):
            ad['nss_info'] = ad['nss_info'].upper()

        if ad.get('kerberos_realm') and type(ad['kerberos_realm']) == dict:
            ad['kerberos_realm'] = ad['kerberos_realm']['id']

        return ad

    @private
    async def ad_compress(self, ad):
        """
        Convert kerberos realm to id. Force domain to upper-case. Remove
        foreign entries.
        kinit will fail if domain name is lower-case.
        """
        for key in ['netbiosname', 'netbiosalias', 'netbiosname_a', 'netbiosname_b']:
            if key in ad:
                ad.pop(key)

        if ad.get('nss_info'):
            ad['nss_info'] = ad['nss_info'].upper()

        return ad

    @accepts()
    async def nss_info_choices(self):
        """
        Returns list of available LDAP schema choices.
        """
        return await self.middleware.call('directoryservices.nss_info_choices', 'ACTIVEDIRECTORY')

    @private
    async def update_netbios_data(self, old, new):
        smb_ha_mode = await self.middleware.call('smb.get_smb_ha_mode')
        must_update = False
        for key in ['netbiosname', 'netbiosalias', 'netbiosname_a', 'netbiosname_b']:
            if key in new and old[key] != new[key]:
                must_update = True if new[key] else False

        if smb_ha_mode == 'STANDALONE' and must_update:
            await self.middleware.call(
                'smb.update',
                {
                    'netbiosname': new['netbiosname'],
                    'netbiosalias': new['netbiosalias']
                }
            )

        elif smb_ha_mode == 'UNIFIED' and must_update:
            await self.middleware.call('smb.update', {'netbiosalias': new['netbiosalias']})
            await self.middleware.call('network.configuration.update', {'hostname_virtual': new['netbiosname']})

        elif smb_ha_mode == 'LEGACY' and must_update:
            await self.middleware.call('smb.update', {'netbiosalias': new['netbiosalias']})
            await self.middleware.call(
                'network.configuration.update',
                {
                    'hostname': new['netbiosname'],
                    'hostname_b': new['netbiosname_b']
                }
            )
        return

    @private
    async def common_validate(self, new, old, verrors):
        if new['kerberos_realm'] and new['kerberos_realm'] != old['kerberos_realm']:
            realm = await self.middleware.call('kerberos.realm.query', [("id", "=", new['kerberos_realm'])])
            if not realm:
                verrors.add(
                    'activedirectory_update.kerberos_realm',
                    'Invalid Kerberos realm id. Realm does not exist.'
                )

        if not new["enable"]:
            return

        ds_state = await self.middleware.call('directoryservices.get_state')
        if ds_state['ldap'] != 'DISABLED':
            verrors.add(
                "activedirectory_update.enable",
                "Active Directory service may not be enabled while LDAP service is enabled."
            )
        if new["enable"] and old["enable"] and new["kerberos_realm"] != old["kerberos_realm"]:
            verrors.add(
                "activedirectory_update.kerberos_realm",
                "Kerberos realm may not be be altered while the AD service is enabled. "
                "This is to avoid introducing possible configuration errors that may result "
                "in a production outage."
            )
        if not new["bindpw"] and not new["kerberos_principal"]:
            verrors.add(
                "activedirectory_update.bindname",
                "Bind credentials or kerberos keytab are required to join an AD domain."
            )
        if new["bindpw"] and new["kerberos_principal"]:
            verrors.add(
                "activedirectory_update.kerberos_principal",
                "Simultaneous keytab and password authentication are not permitted."
            )
        if not new["domainname"]:
            verrors.add(
                "activedirectory_update.domainname",
                "AD domain name is required."
            )

    @accepts(Dict(
        'activedirectory_update',
        Str('domainname', required=True),
        Str('bindname'),
        Str('bindpw', private=True),
        Bool('verbose_logging'),
        Bool('use_default_domain'),
        Bool('allow_trusted_doms'),
        Bool('allow_dns_updates'),
        Bool('disable_freenas_cache'),
        Bool('restrict_pam', default=False),
        Str('site', null=True),
        Int('kerberos_realm', null=True),
        Str('kerberos_principal', null=True),
        Int('timeout', default=60),
        Int('dns_timeout', default=10),
        Str('nss_info', null=True, default='', enum=['SFU', 'SFU20', 'RFC2307']),
        Str('createcomputer'),
        Str('netbiosname'),
        Str('netbiosname_b'),
        List('netbiosalias'),
        Bool('enable'),
        update=True
    ))
    async def do_update(self, data):
        """
        Update active directory configuration.
        `domainname` full DNS domain name of the Active Directory domain.

        `bindname` username used to perform the intial domain join.

        `bindpw` password used to perform the initial domain join. User-
        provided credentials are used to obtain a kerberos ticket, which
        is used to perform the actual domain join.

        `verbose_logging` increase logging during the domain join process.

        `use_default_domain` controls whether domain users and groups have
        the pre-windows 2000 domain name prepended to the user account. When
        enabled, the user appears as "administrator" rather than
        "EXAMPLE\administrator"

        `allow_trusted_doms` enable support for trusted domains. If this
        parameter is enabled, then separate idmap backends _must_ be configured
        for each trusted domain, and the idmap cache should be cleared.

        `allow_dns_updates` during the domain join process, automatically
        generate DNS entries in the AD domain for the NAS. If this is disabled,
        then a domain administrator must manually add appropriate DNS entries
        for the NAS. This parameter is recommended for TrueNAS HA servers.

        `disable_freenas_cache` disables active caching of AD users and groups.
        When disabled, only users cached in winbind's internal cache are
        visible in GUI dropdowns. Disabling active caching is recommended
        in environments with a large amount of users.

        `site` AD site of which the NAS is a member. This parameter is auto-
        detected during the domain join process. If no AD site is configured
        for the subnet in which the NAS is configured, then this parameter
        appears as 'Default-First-Site-Name'. Auto-detection is only performed
        during the initial domain join.

        `kerberos_realm` in which the server is located. This parameter is
        automatically populated during the initial domain join. If the NAS has
        an AD site configured and that site has multiple kerberos servers, then
        the kerberos realm is automatically updated with a site-specific
        configuration to use those servers. Auto-detection is only performed
        during initial domain join.

        `kerberos_principal` kerberos principal to use for AD-related
        operations outside of Samba. After intial domain join, this field is
        updated with the kerberos principal associated with the AD machine
        account for the NAS.

        `nss_info` controls how Winbind retrieves Name Service Information to
        construct a user's home directory and login shell. This parameter
        is only effective if the Active Directory Domain Controller supports
        the Microsoft Services for Unix (SFU) LDAP schema.

        `timeout` timeout value for winbind-related operations. This value may
        need to be increased in  environments with high latencies for
        communications with domain controllers or a large number of domain
        controllers. Lowering the value may cause status checks to fail.

        `dns_timeout` timeout value for DNS queries during the initial domain
        join. This value is also set as the NETWORK_TIMEOUT in the ldap config
        file.

        `createcomputer` Active Directory Organizational Unit in which new
        computer accounts are created.

        The OU string is read from top to bottom without RDNs. Slashes ("/")
        are used as delimiters, like `Computers/Servers/NAS`. The backslash
        ("\\") is used to escape characters but not as a separator. Backslashes
        are interpreted at multiple levels and might require doubling or even
        quadrupling to take effect.

        When this field is blank, new computer accounts are created in the
        Active Directory default OU.

        The Active Directory service is started after a configuration
        update if the service was initially disabled, and the updated
        configuration sets `enable` to `True`. The Active Directory
        service is stopped if `enable` is changed to `False`. If the
        configuration is updated, but the initial `enable` state is `True`, and
        remains unchanged, then the samba server is only restarted.

        During the domain join, a kerberos keytab for the newly-created AD
        machine account is generated. It is used for all future
        LDAP / AD interaction and the user-provided credentials are removed.
        """
        await self.middleware.call("smb.cluster_check")
        verrors = ValidationErrors()
        old = await self.config()
        new = old.copy()
        new.update(data)
        new['domainname'] = new['domainname'].upper()

        try:
            await self.update_netbios_data(old, new)
        except Exception as e:
            raise ValidationError('activedirectory_update.netbiosname', str(e))

        await self.common_validate(new, old, verrors)

        verrors.check()

        if new['enable'] and not old['enable']:
            """
            Currently run two health checks prior to validating domain.
            1) Attempt to kinit with user-provided credentials. This is used to
               verify that the credentials are correct.
            2) Check for an overly large time offset. System kerberos libraries
               may not report the time offset as an error during kinit, but the large
               time offset will prevent libads from using the ticket for the domain
               join.
            """

            try:
                await self.validate_credentials(new)
            except CallError as e:
                if new['kerberos_principal']:
                    method = "activedirectory.kerberos_principal"
                else:
                    method = "activedirectory.bindpw"

                raise ValidationError(
                    method, f'Failed to validate bind credentials: {e.errmsg.split(":")[-1:][0]}'
                )

            try:
                await self.middleware.run_in_thread(self.check_clockskew, new)
            except ntplib.NTPException:
                self.logger.warning("NTP request to Domain Controller failed.",
                                    exc_info=True)
            except Exception as e:
                await self.middleware.call("kerberos.stop")
                raise ValidationError(
                    "activedirectory_update",
                    f"Failed to validate domain configuration: {e}"
                )

        new = await self.ad_compress(new)
        ret = await super().do_update(new)

        diff = await self.diff_conf_and_registry(new)
        await self.middleware.call('sharing.smb.apply_conf_diff', 'GLOBAL', diff)

        job = None
        if not old['enable'] and new['enable']:
            job = (await self.middleware.call('activedirectory.start')).id

        elif not new['enable'] and old['enable']:
            job = (await self.middleware.call('activedirectory.stop')).id

        elif new['enable'] and old['enable']:
            await self.middleware.call('service.restart', 'idmap')

        ret.update({'job_id': job})
        return ret

    @private
    async def diff_conf_and_registry(self, data):
        to_check = {}
        smbconf = (await self.middleware.call('smb.reg_globals'))['ds']
        await self.convert_schema_to_registry(data, to_check)

        r = smbconf
        s_keys = set(to_check.keys())
        r_keys = set(r.keys())
        intersect = s_keys.intersection(r_keys)
        return {
            'added': {x: to_check[x] for x in s_keys - r_keys},
            'removed': {x: r[x] for x in r_keys - s_keys},
            'modified': {x: to_check[x] for x in intersect if to_check[x] != r[x]},
        }

    @private
    async def synchronize(self, data=None):
        if data is None:
            data = await self.config()

        diff = await self.diff_conf_and_registry(data)
        await self.middleware.call('sharing.smb.apply_conf_diff', 'GLOBAL', diff)

    @private
    async def set_state(self, state):
        return await self.middleware.call('directoryservices.set_state', {'activedirectory': state.name})

    @accepts()
    async def get_state(self):
        """
        Wrapper function for 'directoryservices.get_state'. Returns only the state of the
        Active Directory service.
        """
        return (await self.middleware.call('directoryservices.get_state'))['activedirectory']

    @private
    async def set_idmap(self, trusted_domains, our_domain):
        idmap = await self.middleware.call('idmap.query',
                                           [('id', '=', DSType.DS_TYPE_ACTIVEDIRECTORY.value)],
                                           {'get': True})
        idmap_id = idmap.pop('id')
        if not idmap['range_low']:
            idmap['range_low'], idmap['range_high'] = await self.middleware.call('idmap.get_next_idmap_range')
        idmap['dns_domain_name'] = our_domain.upper()
        await self.middleware.call('idmap.update', idmap_id, idmap)
        if trusted_domains:
            await self.middleware.call('idmap.autodiscover_trusted_domains')

    @private
    @job(lock="AD_start_stop")
    async def start(self, job):
        """
        Start AD service. In 'UNIFIED' HA configuration, only start AD service
        on active storage controller.
        """
        await self.middleware.call("smb.cluster_check")
        ad = await self.config()
        smb = await self.middleware.call('smb.config')
        workgroup = smb['workgroup']
        smb_ha_mode = await self.middleware.call('smb.reset_smb_ha_mode')
        if smb_ha_mode == 'UNIFIED':
            if await self.middleware.call('failover.status') != 'MASTER':
                return

        state = await self.get_state()
        if state in [DSStatus['JOINING'], DSStatus['LEAVING']]:
            raise CallError(f'Active Directory Service has status of [{state}]. Wait until operation completes.', errno.EBUSY)

        await self.set_state(DSStatus['JOINING'])
        job.set_progress(0, 'Preparing to join Active Directory')
        if ad['verbose_logging']:
            self.logger.debug('Starting Active Directory service for [%s]', ad['domainname'])
        await super().do_update({'enable': True})
        await self.synchronize()
        await self.middleware.call('etc.generate', 'hostname')

        """
        Kerberos realm field must be populated so that we can perform a kinit
        and use the kerberos ticket to execute 'net ads' commands.
        """
        job.set_progress(5, 'Configuring Kerberos Settings.')
        if not ad['kerberos_realm']:
            realms = await self.middleware.call('kerberos.realm.query', [('realm', '=', ad['domainname'])])

            if realms:
                realm_id = realms[0]['id']
            else:
                realm_id = await self.middleware.call('kerberos.realm.direct_create',
                                                      {'realm': ad['domainname'].upper()})

            await self.direct_update({"kerberos_realm": realm_id})
            ad = await self.config()

        if not await self.middleware.call('kerberos._klist_test'):
            await self.middleware.call('kerberos.start')

        """
        'workgroup' is the 'pre-Windows 2000 domain name'. It must be set to the nETBIOSName value in Active Directory.
        This must be properly configured in order for Samba to work correctly as an AD member server.
        'site' is the ad site of which the NAS is a member. If sites and subnets are unconfigured this will
        default to 'Default-First-Site-Name'.
        """

        job.set_progress(20, 'Detecting Active Directory Site.')
        if not ad['site']:
            new_site = await self.get_site()
            if new_site and new_site != 'Default-First-Site-Name':
                ad['site'] = new_site
                await self.middleware.call('activedirectory.set_kerberos_servers', ad)

        job.set_progress(30, 'Detecting Active Directory NetBIOS Domain Name.')
        if not workgroup or workgroup == 'WORKGROUP':
            workgroup = await self.middleware.call('activedirectory.get_netbios_domain_name', smb['workgroup'])

        await self.middleware.call('smb.initialize_globals')

        """
        Check response of 'net ads testjoin' to determine whether the server needs to be joined to Active Directory.
        Only perform the domain join if we receive the exact error code indicating that the server is not joined to
        Active Directory. 'testjoin' will fail if the NAS boots before the domain controllers in the environment.
        In this case, samba should be started, but the directory service reported in a FAULTED state.
        """

        job.set_progress(40, 'Performing testjoin to Active Directory Domain')
        ret = await self._net_ads_testjoin(workgroup, ad)
        if ret == neterr.NOTJOINED:
            job.set_progress(50, 'Joining Active Directory Domain')
            self.logger.debug(f"Test join to {ad['domainname']} failed. Performing domain join.")
            await self._net_ads_join(ad)
            await self._register_virthostname(ad, smb, smb_ha_mode)
            if smb_ha_mode != 'LEGACY':
                """
                Manipulating the SPN entries must be done with elevated privileges. Add NFS service
                principals while we have these on-hand.
                Since this may potentially take more than a minute to complete, run in background job.
                """
                job.set_progress(60, 'Adding NFS Principal entries.')
                # Skip health check for add_nfs_spn since by this point our AD join should be de-facto healthy.
                spn_job = await self.middleware.call('activedirectory.add_nfs_spn', ad, False, False)
                await spn_job.wait()

                job.set_progress(70, 'Storing computer account keytab.')
                kt_id = await self.middleware.call('kerberos.keytab.store_samba_keytab')
                if kt_id:
                    self.logger.debug('Successfully generated keytab for computer account. Clearing bind credentials')
                    ad = await self.direct_update({
                        'bindpw': '',
                        'kerberos_principal': f'{ad["netbiosname"].upper()}$@{ad["domainname"]}'
                    })

            ret = neterr.JOINED

            job.set_progress(80, 'Configuring idmap backend and NTP servers.')
            await self.middleware.call('service.update', 'cifs', {'enable': True})
            await self.set_idmap(ad['allow_trusted_doms'], ad['domainname'])
            await self.middleware.call('activedirectory.set_ntp_servers')

        job.set_progress(90, 'Restarting SMB server.')
        await self.middleware.call('idmap.synchronize')
        await self.middleware.call('service.restart', 'cifs')
        await self.middleware.call('etc.generate', 'pam')
        if ret == neterr.JOINED:
            await self.set_state(DSStatus['HEALTHY'])
            await self.middleware.call('admonitor.start')
            await self.middleware.call('service.start', 'dscache')
            if ad['verbose_logging']:
                self.logger.debug('Successfully started AD service for [%s].', ad['domainname'])

            if smb_ha_mode == "LEGACY" and (await self.middleware.call('failover.status')) == 'MASTER':
                job.set_progress(95, 'starting active directory on standby controller')
                try:
                    await self.middleware.call('failover.call_remote', 'activedirectory.start')
                except Exception:
                    self.logger.warning('Failed to start active directory service on standby controller', exc_info=True)
        else:
            await self.set_state(DSStatus['FAULTED'])
            self.logger.warning('Server is joined to domain [%s], but is in a faulted state.', ad['domainname'])

        if smb_ha_mode == 'CLUSTERED':
            cl_reload = await self.middleware.call('clusterjob.submit', 'activedirectory.cluster_reload')
            await cl_reload.wait()

        job.set_progress(100, f'Active Directory start completed with status [{ret.name}]')
        return ret.name

    @private
    @job(lock="AD_start_stop")
    async def stop(self, job):
        job.set_progress(0, 'Preparing to stop Active Directory service')
        await self.middleware.call("smb.cluster_check")
        await self.direct_update({"enable": False})

        await self.set_state(DSStatus['LEAVING'])
        job.set_progress(5, 'Stopping Active Directory monitor')
        await self.middleware.call('admonitor.stop')
        await self.middleware.call('etc.generate', 'hostname')
        job.set_progress(10, 'Stopping kerberos service')
        await self.middleware.call('kerberos.stop')
        job.set_progress(20, 'Reconfiguring SMB.')
        await self.synchronize()
        await self.middleware.call('idmap.synchronize')
        await self.middleware.call('service.restart', 'cifs')
        job.set_progress(40, 'Reconfiguring pam and nss.')
        await self.middleware.call('etc.generate', 'pam')
        await self.set_state(DSStatus['DISABLED'])
        job.set_progress(60, 'clearing caches.')
        await self.middleware.call('service.stop', 'dscache')
        flush = await run([SMBCmd.NET.value, "cache", "flush"], check=False)
        if flush.returncode != 0:
            self.logger.warning("Failed to flush samba's general cache after stopping Active Directory service.")

        smb_ha_mode = await self.middleware.call('smb.reset_smb_ha_mode')
        if smb_ha_mode == "LEGACY" and (await self.middleware.call('failover.status')) == 'MASTER':
            job.set_progress(70, 'Propagating changes to standby controller.')
            try:
                await self.middleware.call('failover.call_remote', 'activedirectory.stop')
            except Exception:
                self.logger.warning('Failed to stop active directory service on standby controller', exc_info=True)

        smb_ha_mode = await self.middleware.call('smb.get_smb_ha_mode')
        if smb_ha_mode == 'CLUSTERED':
            job.set_progress(70, 'Propagating changes to cluster.')
            await self.middleware.call('clusterjob.submit', 'activedirectory.cluster_reload')

        await self.set_state(DSStatus['DISABLED'])
        job.set_progress(100, 'Active Directory stop completed.')

    @private
    async def cluster_reload(self):
        enabled = (await self.config())['enable']
        await self.middleware.call('etc.generate', 'hostname')
        await self.middleware.call('etc.generate', 'pam')
        await self.middleware.call('service.restart', 'cifs')
        verb = "start" if enabled else "stop"
        await self.middleware.call(f'kerberos.{verb}')
        await self.middleware.call(f'service.{verb}', 'dscache')

    @private
    async def validate_credentials(self, ad=None):
        """
        Kinit with user-provided credentials is sufficient to determine
        whether the credentials are good. A testbind here is unnecessary.
        """
        if await self.middleware.call('kerberos._klist_test'):
            # Short-circuit credential validation if we have a valid tgt
            return

        if ad is None:
            ad = await self.middleware.call('activedirectory.config')

        payload = {
            'dstype': DSType.DS_TYPE_ACTIVEDIRECTORY.name,
            'conf': {
                'bindname': ad['bindname'],
                'bindpw': ad['bindpw'],
                'domainname': ad['domainname'],
                'kerberos_principal': ad['kerberos_principal'],
            }
        }
        cred = await self.middleware.call('kerberos.get_cred', payload)
        await self.middleware.call('kerberos.do_kinit', {'krb5_cred': cred})
        return

    @private
    def check_clockskew(self, ad=None):
        """
        Uses DNS srv records to determine server with PDC emulator FSMO role and
        perform NTP query to determine current clockskew. Raises exception if
        clockskew exceeds 3 minutes, otherwise returns dict with hostname of
        PDC emulator, time as reported from PDC emulator, and time difference
        between the PDC emulator and the NAS.
        """
        permitted_clockskew = datetime.timedelta(minutes=3)
        nas_time = datetime.datetime.now()

        try:
            lookup = self.middleware.call_sync('activedirectory.lookup_dc')
        except CallError as e:
            self.logger.warning(e.errmsg)
            return {'pdc': None, 'timestamp': '0', 'clockskew': 0}

        pdc = lookup["Information for Domain Controller"]

        c = ntplib.NTPClient()
        response = c.request(pdc)
        ntp_time = datetime.datetime.fromtimestamp(response.tx_time)
        clockskew = abs(ntp_time - nas_time)
        if clockskew > permitted_clockskew:
            raise CallError(f'Clockskew between {pdc} and NAS exceeds 3 minutes: {clockskew}')
        return {'pdc': pdc, 'timestamp': str(ntp_time), 'clockskew': str(clockskew)}

    @private
    def validate_domain(self, data=None):
        """
        Methods used to determine AD domain health.
        First we check whether our clock offset has grown to potentially production-impacting
        levels, then we change whether another DC in our AD site is able to take over if the
        DC winbind is currently connected to becomes inaccessible.
        """
        self.middleware.call_sync('activedirectory.check_clockskew', data)
        self.conn_check(data)

    @private
    def conn_check(self, data=None, dc=None):
        """
        Temporarily connect to netlogon share of a DC that isn't the one that
        winbind is currently communicating with in order to validate our credentials
        and ability to failover in case of outage on winbind's current DC.

        We only check a single DC because domains can have a significantly large number
        of domain controllers in a given site.
        """
        if data is None:
            data = self.middleware.call_sync("activedirectory.config")
        if dc is None:
            AD_DNS = ActiveDirectory_DNS(conf=data, logger=self.logger)
            res = AD_DNS.get_n_working_servers(SRV['DOMAINCONTROLLER'], 2)
            if len(res) != 2:
                self.logger.warning("Less than two Domain Controllers are in our "
                                    "Active Directory Site. This may result in production "
                                    "outage if the currently connected DC is unreachable.")
                return False

            """
            In some pathologically bad cases attempts to get the DC that winbind is currently
            communicating with can time out. For this particular health check, the winbind
            error should not be considered fatal.
            """
            wb_dcinfo = subprocess.run([SMBCmd.WBINFO.value, "--dc-info", data["domainname"]],
                                       capture_output=True, check=False)
            if wb_dcinfo.returncode == 0:
                # output "FQDN (ip address)"
                our_dc = wb_dcinfo.stdout.decode().split()[0]
                for dc_to_check in res:
                    thehost = dc_to_check['host']
                    if thehost.casefold() != our_dc.casefold():
                        dc = thehost
            else:
                self.logger.warning("Failed to get DC info from winbindd: %s", wb_dcinfo.stderr.decode())
                dc = res[0]['host']

        return True

    @accepts()
    async def started(self):
        """
        Issue a no-effect command to our DC. This checks if our secure channel connection to our
        domain controller is still alive. It has much less impact than wbinfo -t.
        Default winbind request timeout is 60 seconds, and can be adjusted by the smb4.conf parameter
        'winbind request timeout ='
        """
        verrors = ValidationErrors()
        config = await self.config()
        if not config['enable']:
            await self.set_state(DSStatus['DISABLED'])
            return False

        await self.common_validate(config, config, verrors)

        try:
            verrors.check()
        except Exception:
            await self.direct_update({"enable": False})
            raise CallError('Automatically disabling ActiveDirectory service due to invalid configuration.',
                            errno.EINVAL)

        """
        Initialize state to "JOINING" until after booted.
        """
        if not await self.middleware.call('system.ready'):
            await self.set_state(DSStatus['JOINING'])
            return True

        """
        Verify winbindd netlogon connection.
        """
        netlogon_ping = await run([SMBCmd.WBINFO.value, '-P'], check=False)
        if netlogon_ping.returncode != 0:
            wberr = netlogon_ping.stderr.decode().strip('\n')
            err = errno.EFAULT
            for wb in WBCErr:
                if wb.err() in wberr:
                    wberr = wberr.replace(wb.err(), wb.value[0])
                    err = wb.value[1] if wb.value[1] else errno.EFAULT
                    break

            raise CallError(wberr, err)

        if (await self.middleware.call('smb.get_smb_ha_mode')) == 'CLUSTERED':
            state_method = 'clustercache.get'
        else:
            state_method = 'cache.get'

        try:
            cached_state = await self.middleware.call(state_method, 'DS_STATE')

            if cached_state['activedirectory'] != 'HEALTHY':
                await self.set_state(DSStatus['HEALTHY'])
        except KeyError:
            await self.set_state(DSStatus['HEALTHY'])

        return True

    @private
    async def _register_virthostname(self, ad, smb, smb_ha_mode):
        """
        This co-routine performs virtual hostname aware
        dynamic DNS updates after joining AD to register
        VIP addresses.
        """
        if not ad['allow_dns_updates'] or smb_ha_mode in ['STANDALONE', 'CLUSTERED']:
            return

        vhost = (await self.middleware.call('network.configuration.config'))['hostname_virtual']
        vips = [i['address'] for i in (await self.middleware.call('interface.ip_in_use', {'static': True}))]
        smb_bind_ips = smb['bindip'] if smb['bindip'] else vips
        to_register = set(vips) & set(smb_bind_ips)
        hostname = f'{vhost}.{ad["domainname"]}'
        cmd = [SMBCmd.NET.value, '-k', 'ads', 'dns', 'register', hostname]
        cmd.extend(to_register)
        netdns = await run(cmd, check=False)
        if netdns.returncode != 0:
            self.logger.debug("hostname: %s, ips: %s, text: %s",
                              hostname, to_register, netdns.stderr.decode())

    @private
    async def _parse_join_err(self, msg):
        if len(msg) < 2:
            raise CallError(msg)

        if "Invalid configuration" in msg[1]:
            """
            ./source3/libnet/libnet_join.c will return configuration erros for the
            following situations:
            - incorrect workgroup
            - incorrect realm
            - incorrect security settings
            Unless users set auxiliary parameters, only the first should be a possibility.
            """
            raise CallError(f'{msg[1].rsplit(")",1)[0]}).', errno.EINVAL)
        else:
            raise CallError(msg[1])

    @private
    async def _net_ads_join(self, ad=None):
        await self.middleware.call("kerberos.check_ticket")
        if ad is None:
            ad = await self.config()

        if ad['createcomputer']:
            netads = await run([
                SMBCmd.NET.value, '-k', '-U', ad['bindname'], '-d', '5',
                'ads', 'join', f'createcomputer={ad["createcomputer"]}',
                ad['domainname']], check=False)
        else:
            netads = await run([
                SMBCmd.NET.value, '-k', '-U', ad['bindname'], '-d', '5',
                'ads', 'join', ad['domainname']], check=False)

        if netads.returncode != 0:
            await self.set_state(DSStatus['FAULTED'])
            await self._parse_join_err(netads.stdout.decode().split(':', 1))

    @private
    async def _net_ads_testjoin(self, workgroup, ad=None):
        """
        If neterr.NOTJOINED is returned then we will proceed with joining (or re-joining)
        the AD domain. There are currently two reasons to do this:
        1) we're not joined to AD
        2) our computer account was deleted out from under us
        It's generally better to report an error condition to the end user and let them
        fix it, but situation (2) above is straightforward enough to automatically re-join.
        In this case, the error message presents oddly because stale credentials are stored in
        the secrets.tdb file and the message is passed up from underlying KRB5 library.
        """
        await self.middleware.call("kerberos.check_ticket")
        if ad is None:
            ad = await self.config()

        netads = await run([
            SMBCmd.NET.value, '-k', '-w', workgroup,
            '-d', '5', 'ads', 'testjoin', ad['domainname']],
            check=False
        )
        if netads.returncode != 0:
            errout = netads.stderr.decode()
            with open(f"{SMBPath.LOGDIR.platform()}/domain_testjoin_{int(datetime.datetime.now().timestamp())}.log", "w") as f:
                f.write(errout)

            return neterr.to_status(errout)

        return neterr.JOINED

    @private
    async def _net_ads_setspn(self, spn_list):
        """
        Only automatically add NFS SPN entries on domain join
        if kerberized nfsv4 is enabled.
        """
        if not (await self.middleware.call('nfs.config'))['v4_krb']:
            return False

        for spn in spn_list:
            netads = await run([
                SMBCmd.NET.value, '-k', 'ads', 'setspn',
                'add', spn
            ], check=False)
            if netads.returncode != 0:
                raise CallError('failed to set spn entry '
                                f'[{spn}]: {netads.stdout.decode().strip()}')

        return True

    @accepts()
    async def get_spn_list(self):
        """
        Return list of kerberos SPN entries registered for the server's Active
        Directory computer account. This may not reflect the state of the
        server's current kerberos keytab.
        """
        await self.middleware.call("kerberos.check_ticket")
        spnlist = []
        netads = await run([SMBCmd.NET.value, '-k', 'ads', 'setspn', 'list'], check=False)
        if netads.returncode != 0:
            raise CallError(
                f"Failed to generate SPN list: [{netads.stderr.decode().strip()}]"
            )

        for spn in netads.stdout.decode().splitlines():
            if len(spn.split('/')) != 2:
                continue
            spnlist.append(spn.strip())

        return spnlist

    @accepts()
    async def change_trust_account_pw(self):
        """
        Force an update of the AD machine account password. This can be used to
        refresh the Kerberos principals in the server's system keytab.
        """
        await self.middleware.call("kerberos.check_ticket")
        workgroup = (await self.middleware.call('smb.config'))['workgroup']
        netads = await run([SMBCmd.NET.value, '-k', 'ads', '-w', workgroup, 'changetrustpw'], check=False)
        if netads.returncode != 0:
            raise CallError(
                f"Failed to update trust password: [{netads.stderr.decode().strip()}] "
                f"stdout: [{netads.stdout.decode().strip()}] "
            )

    @private
    @job(lock="spn_manipulation")
    async def add_nfs_spn(self, job, ad=None, check_health=True, update_keytab=False):
        if check_health:
            ad_state = await self.get_state()
            if ad_state != DSStatus.HEALTHY.name:
                raise CallError("Service Principal Names that are registered in Active Directory "
                                "may only be manipulated when the Active Directory Service is Healthy. "
                                f"Current state is: {ad_state}")

        if ad is None:
            ad = await self.config()

        ok = await self._net_ads_setspn([
            f'nfs/{ad["netbiosname"].upper()}.{ad["domainname"]}',
            f'nfs/{ad["netbiosname"].upper()}'
        ])
        if not ok:
            return False

        await self.change_trust_account_pw()
        if update_keytab:
            await self.middleware.call('kerberos.keytab.store_samba_keytab')

        return True

    @accepts()
    async def domain_info(self):
        """
        Returns the following information about the currently joined domain:

        `LDAP server` IP address of current LDAP server to which TrueNAS is connected.

        `LDAP server name` DNS name of LDAP server to which TrueNAS is connected

        `Realm` Kerberos realm

        `LDAP port`

        `Server time` timestamp.

        `KDC server` Kerberos KDC to which TrueNAS is connected

        `Server time offset` current time offset from DC.

        `Last machine account password change`. timestamp
        """
        await self.middleware.call("kerberos.check_ticket")
        netads = await run([SMBCmd.NET.value, '-k', 'ads', 'info', '--json'], check=False)
        if netads.returncode != 0:
            raise CallError(netads.stderr.decode())

        return json.loads(netads.stdout.decode())

    @private
    async def get_netbios_domain_name(self, workgroup=None):
        """
        The 'workgroup' parameter must be set correctly in order for AD join to
        succeed. This is based on the short form of the domain name, which was defined
        by the AD administrator who deployed originally deployed the AD enviornment.
        The only way to reliably get this is to query the LDAP server. This method
        queries and sets it.
        """

        if workgroup is None:
            workgroup = (await self.middleware.call_sync('smb.config'))['workgroup']

        netcmd = await run([SMBCmd.NET.value, 'ads', 'workgroup'], check=False)
        if netcmd.returncode != 0:
            raise CallError(
                "Failed to retrieve netbios domain name from Active Directory: "
                f"{netcmd.stderr.decode().strip}"
            )

        output = netcmd.stdout.decode()
        domain = output.split()[1].strip()

        if domain != workgroup:
            self.logger.debug(f'Updating SMB workgroup to match the short form of the AD domain [{domain}]')
            await self.middleware.call('smb.direct_update', {'workgroup': domain})

        return domain

    @private
    def get_kerberos_servers(self, ad=None):
        """
        This returns at most 3 kerberos servers located in our AD site. This is to optimize
        kerberos configuration for locations where kerberos servers may span the globe and
        have equal DNS weighting. Since a single kerberos server may represent an unacceptable
        single point of failure, fall back to relying on normal DNS queries in this case.
        """
        if ad is None:
            ad = self.middleware.call_sync('activedirectory.config')
        AD_DNS = ActiveDirectory_DNS(conf=ad, logger=self.logger)
        krb_kdc = AD_DNS.get_n_working_servers(SRV['KERBEROSDOMAINCONTROLLER'], 3)
        krb_admin_server = AD_DNS.get_n_working_servers(SRV['KERBEROS'], 3)
        krb_kpasswd_server = AD_DNS.get_n_working_servers(SRV['KPASSWD'], 3)
        kdc = [i['host'] for i in krb_kdc]
        admin_server = [i['host'] for i in krb_admin_server]
        kpasswd = [i['host'] for i in krb_kpasswd_server]
        for servers in [kdc, admin_server, kpasswd]:
            if len(servers) == 1:
                return None

        return {
            'kdc': ' '.join(kdc),
            'admin_server': ' '.join(admin_server),
            'kpasswd_server': ' '.join(kpasswd)
        }

    @private
    def set_kerberos_servers(self, ad=None):
        if not ad:
            ad = self.middleware.call_sync('activedirectory.config')
        site_indexed_kerberos_servers = self.get_kerberos_servers(ad)
        if site_indexed_kerberos_servers:
            self.middleware.call_sync(
                'kerberos.realm.update',
                ad['kerberos_realm'],
                site_indexed_kerberos_servers
            )
            self.middleware.call_sync('etc.generate', 'kerberos')

    @private
    async def set_ntp_servers(self):
        """
        Appropriate time sources are a requirement for an AD environment. By default kerberos authentication
        fails if there is more than a 5 minute time difference between the AD domain and the member server.
        """
        ntp_servers = await self.middleware.call('system.ntpserver.query')
        ntp_pool = 'debian.pool.ntp.org'
        default_ntp_servers = list(filter(lambda x: ntp_pool in x['address'], ntp_servers))
        if len(ntp_servers) != 3 or len(default_ntp_servers) != 3:
            return

        try:
            dc = (await self.lookup_dc())["Information for Domain Controller"]
        except CallError:
            self.logger.warning("Failed to automatically set time source.", exc_info=True)
            return

        try:
            await self.middleware.call('system.ntpserver.create', {'address': dc, 'prefer': True})
        except Exception:
            self.logger.warning('Failed to configure NTP for the Active Directory domain. Additional '
                                'manual configuration may be required to ensure consistent time offset, '
                                'which is required for a stable domain join.', exc_info=True)
        return

    @private
    async def lookup_dc(self, ad=None):
        if ad is None:
            ad = await self.config()

        lookup = await run([SMBCmd.NET.value, '--json', '-S', ad['domainname'], 'ads', 'lookup'], check=False)
        if lookup.returncode != 0:
            raise CallError("Failed to look up Domain Controller information: "
                            f"{lookup.stderr.decode().strip()}")

        out = json.loads(lookup.stdout.decode())
        return out

    @private
    async def get_site(self):
        try:
            lookup = await self.lookup_dc()
        except CallError as e:
            self.logger.warning("Failed to get AD site: %s", e)
            return None

        return lookup["Client Site Name"]

    @accepts(Ref('kerberos_username_password'))
    async def leave(self, data):
        """
        Leave Active Directory domain. This will remove computer
        object from AD and clear relevant configuration data from
        the NAS.
        This requires credentials for appropriately-privileged user.
        Credentials are used to obtain a kerberos ticket, which is
        used to perform the actual removal from the domain.
        """
        ad = await self.config()
        smb_ha_mode = await self.middleware.call('smb.get_smb_ha_mode')

        ad['bindname'] = data.get("username", "")
        ad['bindpw'] = data.get("password", "")
        ad['kerberos_principal'] = ''

        payload = {
            'dstype': DSType.DS_TYPE_ACTIVEDIRECTORY.name,
            'conf': {
                'bindname': data.get('username', ''),
                'bindpw': data.get('password', ''),
                'domainname': ad['domainname'],
                'kerberos_principal': '',
            }
        }

        cred = await self.middleware.call('kerberos.get_cred', payload)
        await self.middleware.call('kerberos.do_kinit', {'krb5_cred': cred})

        netads = await run([SMBCmd.NET.value, '-U', data['username'], '-k', 'ads', 'leave'], check=False)
        if netads.returncode != 0:
            self.logger.warning("Failed to leave domain: %s", netads.stderr.decode())

        if smb_ha_mode != 'LEGACY':
            krb_princ = await self.middleware.call(
                'kerberos.keytab.query',
                [('name', '=', 'AD_MACHINE_ACCOUNT')]
            )
            if krb_princ:
                await self.middleware.call('kerberos.keytab.direct_delete', krb_princ[0]['id'])

        try:
            await self.middleware.call('kerberos.realm.direct_delete', ad['kerberos_realm'])
        except MatchNotFound:
            pass

        if netads.returncode == 0 and smb_ha_mode != 'CLUSTERED':
            try:
                pdir = await self.middleware.call("smb.getparm", "private directory", "GLOBAL")
                ts = time.time()
                os.rename(f"{pdir}/secrets.tdb", f"{pdir}/secrets.tdb.bak.{int(ts)}")
                await self.middleware.call("directoryservices.backup_secrets")
            except Exception:
                self.logger.debug("Failed to remove stale secrets file.", exc_info=True)

        payload = {
            'enable': False,
            'site': None,
            'kerberos_realm': None,
            'kerberos_principal': '',
            'domainname': '',
        }
        new = await self.middleware.call('activedirectory.direct_update', payload)
        await self.set_state(DSStatus['DISABLED'])
        if smb_ha_mode == 'LEGACY' and (await self.middleware.call('failover.status')) == 'MASTER':
            try:
                await self.middleware.call('failover.call_remote', 'activedirectory.leave', [data])
            except Exception:
                self.logger.warning("Failed to leave AD domain on passive storage controller.", exc_info=True)

        flush = await run([SMBCmd.NET.value, "cache", "flush"], check=False)
        if flush.returncode != 0:
            self.logger.warning("Failed to flush samba's general cache after leaving Active Directory.")

        with contextlib.suppress(FileNotFoundError):
            os.unlink('/etc/krb5.keytab')

        await self.middleware.call('kerberos.stop')
        await self.middleware.call('etc.generate', 'pam')
        await self.synchronize(new)
        await self.middleware.call('idmap.synchronize')
        await self.middleware.call('service.restart', 'cifs')
        return

    @private
    def get_gencache_sid(self, tdb_key):
        gencache = tdb.Tdb('/tmp/gencache.tdb', 0, tdb.DEFAULT, os.O_RDONLY)
        try:
            tdb_val = gencache.get(tdb_key)
        finally:
            gencache.close()

        if tdb_val is None:
            return None

        decoded_sid = tdb_val[8:-5].decode()
        if decoded_sid == '-':
            return None

        return decoded_sid

    @private
    def get_gencache_names(self, idmap_domain):
        out = []
        known_doms = [x['domain_info']['name'] for x in idmap_domain]

        gencache = tdb.Tdb('/tmp/gencache.tdb', 0, tdb.DEFAULT, os.O_RDONLY)
        try:
            for k in gencache.keys():
                if k[:8] != b'NAME2SID':
                    continue
                key = k[:-1].decode()
                name = key.split('/', 1)[1]
                dom = name.split('\\')[0]
                if dom not in known_doms:
                    continue

                out.append(name)
        finally:
            gencache.close()

        return out

    @private
    def get_entries(self, data):
        ret = []
        entry_type = data.get('entry_type')
        do_wbinfo = data.get('cache_enabled', True)

        shutil.copyfile(f'{SMBPath.LOCKDIR.platform()}/gencache.tdb', '/tmp/gencache.tdb')

        domain_info = self.middleware.call_sync(
            'idmap.query', [], {'extra': {'additional_information': ['DOMAIN_INFO']}}
        )
        for dom in domain_info.copy():
            if not dom['domain_info']:
                domain_info.remove(dom)

        dom_by_sid = {x['domain_info']['sid']: x for x in domain_info}

        if do_wbinfo:
            wb = subprocess.run(
                [SMBCmd.WBINFO.value, f'-{entry_type[0].lower()}'], capture_output=True
            )
            if wb.returncode != 0:
                raise CallError(f'Failed to retrieve {entry_type} from active directory: '
                                f'{wb.stderr.decode().strip()}')
            entries = wb.stdout.decode().splitlines()

        else:
            entries = self.get_gencache_names(domain_info)

        for i in entries:
            entry = {"id": -1, "sid": None, "nss": None}
            if entry_type == 'USER':
                try:
                    entry["nss"] = pwd.getpwnam(i)
                except KeyError:
                    continue
                entry["id"] = entry["nss"].pw_uid
                tdb_key = f'IDMAP/UID2SID/{entry["id"]}'

            else:
                try:
                    entry["nss"] = grp.getgrnam(i)
                except KeyError:
                    continue
                entry["id"] = entry["nss"].gr_gid
                tdb_key = f'IDMAP/GID2SID/{entry["id"]}'

            """
            Try to look up in gencache before subprocess to wbinfo.
            """
            entry['sid'] = self.get_gencache_sid((tdb_key.encode() + b"\x00"))
            if not entry['sid']:
                entry['sid'] = self.middleware.call_sync('idmap.unixid_to_sid', {
                    'id_type': entry_type,
                    'id': entry['id'],
                })

            entry['domain_info'] = dom_by_sid[entry['sid'].rsplit('-', 1)[0]]
            ret.append(entry)

        return ret

    @private
    @job(lock='fill_ad_cache')
    def fill_cache(self, job, force=False):
        ad = self.middleware.call_sync('activedirectory.config')
        id_type_both_backends = [
            'RID',
            'AUTORID'
        ]

        users = self.get_entries({'entry_type': 'USER', 'cache_enabled': not ad['disable_freenas_cache']})
        for u in users:
            user_data = u['nss']
            rid = int(u['sid'].rsplit('-', 1)[1])

            entry = {
                'id': 100000 + u['domain_info']['range_low'] + rid,
                'uid': user_data.pw_uid,
                'username': user_data.pw_name,
                'unixhash': None,
                'smbhash': None,
                'group': {},
                'home': '',
                'shell': '',
                'full_name': user_data.pw_gecos,
                'builtin': False,
                'email': '',
                'password_disabled': False,
                'locked': False,
                'sudo': False,
                'sudo_nopasswd': False,
                'sudo_commands': [],
                'microsoft_account': False,
                'attributes': {},
                'groups': [],
                'sshpubkey': None,
                'local': False,
                'id_type_both': u['domain_info']['idmap_backend'] in id_type_both_backends,
                'nt_name': None,
                'sid': None,
            }
            self.middleware.call_sync('dscache.insert', self._config.namespace.upper(), 'USER', entry)

        groups = self.get_entries({'entry_type': 'GROUP', 'cache_enabled': not ad['disable_freenas_cache']})
        for g in groups:
            group_data = g['nss']
            rid = int(g['sid'].rsplit('-', 1)[1])

            entry = {
                'id': 100000 + g['domain_info']['range_low'] + rid,
                'gid': group_data.gr_gid,
                'name': group_data.gr_name,
                'group': group_data.gr_name,
                'builtin': False,
                'sudo': False,
                'sudo_nopasswd': False,
                'sudo_commands': [],
                'users': [],
                'local': False,
                'id_type_both': g['domain_info']['idmap_backend'] in id_type_both_backends,
                'nt_name': None,
                'sid': None,
            }
            self.middleware.call_sync('dscache.insert', self._config.namespace.upper(), 'GROUP', entry)

    @private
    async def get_cache(self):
        users = await self.middleware.call('dscache.entries', self._config.namespace.upper(), 'USER')
        groups = await self.middleware.call('dscache.entries', self._config.namespace.upper(), 'GROUP')
        return {"USERS": users, "GROUPS": groups}


class WBStatusThread(threading.Thread):
    def __init__(self, **kwargs):
        super(WBStatusThread, self).__init__()
        self.setDaemon(True)
        self.middleware = kwargs.get('middleware')
        self.logger = self.middleware.logger
        self.finished = threading.Event()
        self.state = DSStatus.FAULTED.value

    def parse_msg(self, data):
        if data == str(DSStatus.LEAVING.value):
            return

        try:
            m = json.loads(data)
        except json.decoder.JSONDecodeError:
            self.logger.debug("Unable to decode winbind status message: "
                              "%s", data)
            return

        new_state = self.state

        if not self.middleware.call_sync('activedirectory.config')['enable']:
            self.logger.debug('Ignoring winbind message for disabled AD service: [%s]', m)
            return

        try:
            new_state = DSStatus(m['winbind_message']).value
        except Exception as e:
            self.logger.debug('Received invalid winbind status message [%s]: %s', m, e)
            return

        if m['domain_name_netbios'] != self.middleware.call_sync('smb.config')['workgroup']:
            self.logger.debug(
                'Domain [%s] changed state to %s',
                m['domain_name_netbios'],
                DSStatus(m['winbind_message']).name
            )
            return

        if self.state != new_state:
            self.logger.debug(
                'State of domain [%s] transistioned to [%s]',
                m['forest_name'], DSStatus(m['winbind_message'])
            )
            self.middleware.call_sync('activedirectory.set_state', DSStatus(m['winbind_message']))
            if new_state == DSStatus.FAULTED.value:
                self.middleware.call_sync(
                    "alert.oneshot_create",
                    "ActiveDirectoryDomainOffline",
                    {"domain": m["domain_name_netbios"]}
                )
            else:
                self.middleware.call_sync(
                    "alert.oneshot_delete",
                    "ActiveDirectoryDomainOffline",
                    {"domain": m["domain_name_netbios"]}
                )

        self.state = new_state

    def read_messages(self):
        while not self.finished.is_set():
            with open(f'{SMBPath.RUNDIR.platform()}/.wb_fifo') as f:
                data = f.read()
                for msg in data.splitlines():
                    self.parse_msg(msg)

        self.logger.debug('exiting winbind messaging thread')

    def run(self):
        osc.set_thread_name('ad_monitor_thread')
        try:
            self.read_messages()
        except Exception as e:
            self.logger.debug('Failed to run monitor thread %s', e, exc_info=True)

    def setup(self):
        if not os.path.exists(f'{SMBPath.RUNDIR.platform()}/.wb_fifo'):
            os.mkfifo(f'{SMBPath.RUNDIR.platform()}/.wb_fifo')

    def cancel(self):
        """
        Write to named pipe to unblock open() in thread and exit cleanly.
        """
        self.finished.set()
        with open(f'{SMBPath.RUNDIR.platform()}/.wb_fifo', 'w') as f:
            f.write(str(DSStatus.LEAVING.value))


class ADMonitorService(Service):
    class Config:
        private = True

    def __init__(self, *args, **kwargs):
        super(ADMonitorService, self).__init__(*args, **kwargs)
        self.thread = None
        self.initialized = False
        self.lock = threading.Lock()

    def start(self):
        if not self.middleware.call_sync('activedirectory.config')['enable']:
            self.logger.trace('Active directory is disabled. Exiting AD monitoring.')
            return

        with self.lock:
            if self.initialized:
                return

            thread = WBStatusThread(
                middleware=self.middleware,
            )
            thread.setup()
            self.thread = thread
            thread.start()
            self.initialized = True

    def stop(self):
        thread = self.thread
        if thread is None:
            return

        thread.cancel()
        self.thread = None

        with self.lock:
            self.initialized = False

    def restart(self):
        self.stop()
        self.start()


async def setup(middleware):
    """
    During initial boot let smb_configure script start monitoring once samba's
    rundir is created.
    """
    if await middleware.call('system.ready'):
        await middleware.call('admonitor.start')
