// Runs a Claude Desktop extension's Node.js server the way Claude Desktop's built-in Node.js host does: in that
// host (an Electron utility process) the server's process.stdin and process.stdout are streams Claude feeds and
// reads, one JSON-RPC message per line; the process's real stdin is closed and its real stdout is only a log.
// Used by check_connector.py and the tests to check the Fortis Ledger extension.
//
//     node claude_node_host.js <messages.jsonl> <replies.jsonl> <server entry .js> [server args...]
//
// Each line of messages.jsonl is sent to the server as Claude sends it; every line the server writes to its
// stdout goes to replies.jsonl.  After the last message the input ends, so a server that stops at the end of
// its input lets this exit.  Start it with its real stdin closed.
"use strict";
const fs = require("fs");
const path = require("path");
const { Readable } = require("stream");
const { StringDecoder } = require("string_decoder");
const { pathToFileURL } = require("url");

const [messagesFile, repliesFile, entry, ...args] = process.argv.slice(2);
if (!entry) {
  console.error("usage: node claude_node_host.js <messages.jsonl> <replies.jsonl> <server entry .js> [args...]");
  process.exit(2);
}

const replies = fs.openSync(repliesFile, "w");
const decoder = new StringDecoder("utf8");
process.stdout.write = function (chunk, encoding, callback) {
  const text = typeof chunk === "string" ? chunk : decoder.write(Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength));
  if (text.length > 0) fs.writeSync(replies, text);
  const done = typeof encoding === "function" ? encoding : callback;
  if (done) process.nextTick(done);
  return true;
};

const input = new Readable({ read() {} });
for (const name of ["read", "push", "unshift", "pause", "resume", "pipe", "unpipe", "on", "once", "removeListener",
                    "removeAllListeners", "setEncoding", "destroy", "isPaused"]) {
  if (typeof input[name] === "function") process.stdin[name] = input[name].bind(input);
}
for (const line of fs.readFileSync(messagesFile, "utf8").split(/\r?\n/)) {
  if (line.trim()) input.push(line.trim() + "\n");
}
input.push(null);

process.argv = [process.platform === "win32" ? "node.exe" : "node", entry, ...args];
import(pathToFileURL(path.resolve(entry)).toString()).catch((err) => {
  console.error(`could not load ${entry}: ${err.stack || err}`);
  process.exit(1);
});
