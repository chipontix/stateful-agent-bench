#!/usr/bin/env bash
# Single-node KRaft Kafka 3.9.1 on the host JVM, for machines whose Docker VM
# is too loaded for a broker (see README "Infrastructure"). Same port and
# settings as the compose service. Usage: infra/kafka-native.sh start|stop
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
k=$root/.cache/kafka data=$root/.cache/kafka-data port=${SAB_KAFKA_PORT:-23092}
case ${1:-start} in
start)
  if [ ! -x "$k/bin/kafka-server-start.sh" ]; then
    mkdir -p "$root/.cache"
    curl -fsSL https://archive.apache.org/dist/kafka/3.9.1/kafka_2.13-3.9.1.tgz -o "$root/.cache/kafka.tgz"
    echo "1ea204ba73411737a275429ca976d440f007ff0957b90b19be41dc5a4bae52617267769be9f0b5791714d0b3c4c760605bd426faea39edd90763585523fa2cfe  $root/.cache/kafka.tgz" | shasum -a 512 -c -
    tar -xzf "$root/.cache/kafka.tgz" -C "$root/.cache" && mv "$root/.cache/kafka_2.13-3.9.1" "$k" && rm "$root/.cache/kafka.tgz"
  fi
  rm -rf "$data" && mkdir -p "$data"
  cat > "$data/server.properties" <<P
process.roles=broker,controller
node.id=1
controller.quorum.voters=1@localhost:$((port + 1))
listeners=PLAINTEXT://localhost:$port,CONTROLLER://localhost:$((port + 1))
advertised.listeners=PLAINTEXT://localhost:$port
controller.listener.names=CONTROLLER
listener.security.protocol.map=PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT
log.dirs=$data/logs
offsets.topic.replication.factor=1
offsets.topic.num.partitions=8
transaction.state.log.replication.factor=1
transaction.state.log.min.isr=1
auto.create.topics.enable=false
group.initial.rebalance.delay.ms=0
log.retention.ms=3600000
log.segment.bytes=16777216
log.index.size.max.bytes=1048576
P
  "$k/bin/kafka-storage.sh" format -t "$("$k/bin/kafka-storage.sh" random-uuid)" -c "$data/server.properties" >/dev/null
  KAFKA_HEAP_OPTS="-Xms1g -Xmx1g" LOG_DIR=$data/app-logs nohup "$k/bin/kafka-server-start.sh" "$data/server.properties" >"$data/stdout.log" 2>&1 &
  echo $! > "$data/pid"
  for _ in $(seq 60); do
    "$k/bin/kafka-topics.sh" --bootstrap-server "localhost:$port" --list >/dev/null 2>&1 && { echo "kafka up on :$port"; exit 0; }
    sleep 1
  done
  echo "kafka did not come up, see $data/stdout.log" >&2; exit 1 ;;
stop)
  [ -f "$data/pid" ] && kill "$(cat "$data/pid")" 2>/dev/null || true
  rm -rf "$data" ;;
esac
