#
# Copyright (C) 2015  FreeIPA Contributors see COPYING for license
#

"""
KRA installer module
"""

from __future__ import absolute_import

import logging
import os

from ipalib import api
from ipalib.kinit import kinit_keytab
from ipaplatform import services
from ipaplatform.paths import paths
from ipapython import ipautil
from ipapython.admintool import ScriptError
from ipapython.install.core import group
from ipaserver.install import ca, cainstance
from ipaserver.install import krainstance
from ipaserver.install import dsinstance
from ipaserver.install import installutils
from ipaserver.install import service as _service

from . import dogtag

logger = logging.getLogger(__name__)


def install_check(api, replica_config, options):
    if replica_config is not None and not replica_config.setup_kra:
        return

    kra = krainstance.KRAInstance(api.env.realm)
    if kra.is_installed():
        raise RuntimeError("KRA is already installed.")

    if not options.setup_ca:
        if cainstance.is_ca_installed_locally():
            if api.env.dogtag_version >= 10:
                # correct dogtag version of CA installed
                pass
            else:
                raise RuntimeError(
                    "Dogtag must be version 10.2 or above to install KRA")
        else:
            raise RuntimeError(
                "Dogtag CA is not installed.  Please install the CA first")

    if replica_config is not None:
        if not api.Command.kra_is_enabled()['result']:
            raise RuntimeError(
                "KRA is not installed on the master system. Please use "
                "'ipa-kra-install' command to install the first instance.")

    if api.env.ca_host is not None and api.env.ca_host != api.env.host:
        raise RuntimeError(
            "KRA can not be installed when 'ca_host' is overriden in "
            "IPA configuration file.")

    # There are three scenarios for installing a KRA
    #   1. At install time of the initial server
    #   2. Using ipa-kra-install
    #   3. At install time of a replica
    #
    # These tests are done in reverse order. If we are doing a
    # replica install we can check the remote CA.
    #
    # If we are running ipa-kra-install then there must be a CA
    # use that.
    #
    # If initial install we either have the token options or we don't.

    cai = cainstance.CAInstance()
    if replica_config is not None:
        (token_name, token_library_path) = ca.lookup_hsm_configuration(api)
    elif cai.is_configured() and cai.hsm_enabled:
        (token_name, token_library_path) = ca.lookup_hsm_configuration(api)
    elif 'token_name' in options.__dict__:
        token_name = options.token_name
        token_library_path = options.token_library_path
    else:
        token_name = None

    if replica_config is not None:
        if (
            token_name
            and options.token_password_file
            and options.token_password
        ):
            raise ScriptError(
                "token-password and token-password-file are mutually exclusive"
            )

    if options.token_password_file:
        with open(options.token_password_file, "r") as fd:
            options.token_password = fd.readline().strip()

    if (
        token_name
        and not options.token_password_file
        and not options.token_password
    ):
        if options.unattended:
            raise ScriptError("HSM token password required")
        token_password = installutils.read_password(
            f"HSM token '{token_name}'", confirm=False
        )
        if token_password is None:
            raise ScriptError("HSM token password required")
        else:
            options.token_password = token_password

    if token_name:
        ca.hsm_validator(token_name, token_library_path, options.token_password)


def install(api, replica_config, options, custodia):
    if replica_config is None:
        if not options.setup_kra:
            return
        realm_name = api.env.realm
        dm_password = options.dm_password
        host_name = api.env.host
        subject_base = dsinstance.DsInstance().find_subject_base()

        pkcs12_info = None
        master_host = None
        promote = False
    else:
        if not replica_config.setup_kra:
            return
        cai = cainstance.CAInstance()
        if not cai.hsm_enabled:
            krafile = os.path.join(replica_config.dir, 'kracert.p12')
            with ipautil.private_ccache():
                ccache = os.environ['KRB5CCNAME']
                kinit_keytab(
                    'host/{env.host}@{env.realm}'.format(env=api.env),
                    paths.KRB5_KEYTAB,
                    ccache)
                custodia.get_kra_keys(
                    krafile,
                    replica_config.dirman_password)
        else:
            krafile = None

        realm_name = replica_config.realm_name
        dm_password = replica_config.dirman_password
        host_name = replica_config.host_name
        subject_base = replica_config.subject_base

        pkcs12_info = (krafile,)
        master_host = replica_config.kra_host_name
        promote = True

    ca_subject = ca.lookup_ca_subject(api, subject_base)

    kra = krainstance.KRAInstance(realm_name)
    kra.configure_instance(
        realm_name, host_name, dm_password, dm_password,
        subject_base=subject_base,
        ca_subject=ca_subject,
        pkcs12_info=pkcs12_info,
        master_host=master_host,
        promote=promote,
        pki_config_override=options.pki_config_override,
        token_password=options.token_password
    )

    _service.print_msg("Restarting the directory server")
    ds = dsinstance.DsInstance()
    ds.restart()
    kra.enable_client_auth_to_db()

    # Restart apache for new proxy config file
    services.knownservices.httpd.restart(capture_output=True)
    # Restarted named to restore bind-dyndb-ldap operation, see
    # https://pagure.io/freeipa/issue/5813
    named = services.knownservices.named  # alias for current named
    if named.is_running():
        named.restart(capture_output=True)


def uninstall_check(options):
    """IPA needs to be running so pkidestroy can unregister KRA"""
    kra = krainstance.KRAInstance(api.env.realm)
    if not kra.is_installed():
        return

    result = ipautil.run([paths.IPACTL, 'status'],
                         raiseonerr=False)

    if result.returncode not in [0, 4]:
        try:
            logger.info(
                "Starting services to unregister KRA from security domain")
            ipautil.run([paths.IPACTL, 'start'])
        except Exception:
            logger.info("Re-starting IPA failed, continuing uninstall")


def uninstall():
    kra = krainstance.KRAInstance(api.env.realm)
    kra.stop_tracking_certificates()
    if kra.is_installed():
        kra.uninstall()


@group
class KRAInstallInterface(dogtag.DogtagInstallInterface):
    """
    Interface of the KRA installer

    Knobs defined here will be available in:
    * ipa-server-install
    * ipa-replica-prepare
    * ipa-replica-install
    * ipa-kra-install
    """
    description = "KRA"
