#!/bin/bash -eu

HTTPD_SYSTEMD_NAME='apache2.service'
HTTPD_LOGDIR='/var/log/apache2'
HTTPD_ERRORLOG="${HTTPD_LOGDIR}/error.log"
HTTPD_BASEDIR='/etc/apache2'
HTTPD_ALIASDIR="${HTTPD_BASEDIR}/alias"
BIND_BASEDIR='/var/cache/bind'
BIND_DATADIR="${BIND_BASEDIR}"

function firewalld_cmd() {
    firewall-cmd $@
}
