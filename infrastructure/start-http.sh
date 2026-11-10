#!/bin/sh
set -eu
. /opt/nifi/scripts/common.sh
for jar in "${NIFI_HOME}"/ex_lib/*.jar; do
    if [ -f "$jar" ]; then cp "$jar" "${NIFI_HOME}/lib/"; fi
done
prop_replace 'java.arg.2' "-Xms${NIFI_JVM_HEAP_INIT:-1g}" "$nifi_bootstrap_file"
prop_replace 'java.arg.3' "-Xmx${NIFI_JVM_HEAP_MAX:-2g}" "$nifi_bootstrap_file"
prop_replace 'nifi.web.http.port' '8088'
prop_replace 'nifi.web.http.host' '0.0.0.0'
prop_replace 'nifi.web.https.port' ''
prop_replace 'nifi.web.https.host' ''
prop_replace 'nifi.security.keystore' ''
prop_replace 'nifi.security.keystoreType' ''
prop_replace 'nifi.security.truststore' ''
prop_replace 'nifi.security.truststoreType' ''
prop_replace 'nifi.remote.input.secure' 'false'
prop_replace 'nifi.cluster.protocol.is.secure' 'false'
prop_replace 'nifi.cluster.is.node' 'false'
prop_replace 'nifi.nar.library.autoload.directory' "${NIFI_HOME}/nar_extensions"
exec "${NIFI_HOME}/bin/nifi.sh" run
