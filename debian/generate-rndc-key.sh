#!/bin/bash

. /lib/lsb/init-functions

# This script generates /etc/rndc.key if doesn't exist AND if there is no rndc.conf

if [ ! -s /etc/rndc.key -a ! -s /etc/rndc.conf ]; then
  echo -n $"Generating /etc/bind/rndc.key:"
  if /usr/sbin/rndc-confgen -a -r /dev/urandom > /dev/null 2>&1; then
    chmod 640 /etc/bind/rndc.key
    chown root.bind /etc/bind/rndc.key
    [ -x /sbin/restorecon ] && /sbin/restorecon /etc/bind/rndc.key
    log_success_msg "/etc/bind/rndc.key generation"
    echo
  else
    log_failure_msg $"/etc/bind/rndc.key generation"
    echo
  fi
fi
