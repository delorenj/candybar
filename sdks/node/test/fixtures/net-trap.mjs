// Preloaded with `node --import` by test/process.test.js: any attempt to open
// a network connection (raw TCP, TLS, UDP, DNS, fetch) aborts the process
// loudly. A NATS client cannot connect without passing through one of these.
import net from "node:net";
import tls from "node:tls";
import dgram from "node:dgram";
import dns from "node:dns";

function trip(what) {
  process.stderr.write(`NETWORK TRAP: ${what}\n`);
  process.exit(99);
}

net.Socket.prototype.connect = function trappedConnect() {
  trip("net.Socket#connect");
};
tls.connect = () => trip("tls.connect");
dgram.createSocket = () => trip("dgram.createSocket");
dns.lookup = () => trip("dns.lookup");
dns.promises.lookup = async () => trip("dns.promises.lookup");
globalThis.fetch = async () => trip("fetch");
