#!/usr/bin/env node

import readline from "node:readline";
import { Codex } from "@openai/codex-sdk";

function writeEvent(event) {
  process.stdout.write(`${JSON.stringify(event)}\n`);
}

function compactText(value, limit = 180) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length <= limit ? text : `${text.slice(0, limit - 3)}...`;
}

function summarizeItem(item, phase) {
  if (!item || typeof item !== "object") return "";
  switch (item.type) {
    case "command_execution": {
      const command = compactText(item.command, 120);
      if (phase === "started") return `Running command: ${command}`;
      if (phase === "completed") {
        const status = item.status || "completed";
        return `${status === "failed" ? "Command failed" : "Command completed"}: ${command}`;
      }
      return "";
    }
    case "mcp_tool_call": {
      const toolName = [item.server, item.tool].filter(Boolean).join(".");
      if (!toolName) return "";
      if (phase === "started") return `Calling MCP tool: ${toolName}`;
      if (phase === "completed") {
        const status = item.status || "completed";
        return `${status === "failed" ? "MCP tool failed" : "MCP tool completed"}: ${toolName}`;
      }
      return "";
    }
    case "web_search":
      return item.query ? `Web search: ${compactText(item.query, 120)}` : "";
    case "todo_list": {
      const items = Array.isArray(item.items) ? item.items : [];
      const complete = items.filter((entry) => entry && entry.completed).length;
      return items.length ? `Todo list updated: ${complete}/${items.length} complete` : "";
    }
    case "file_change": {
      const changes = Array.isArray(item.changes) ? item.changes : [];
      return changes.length ? `Applied ${changes.length} file change(s)` : "";
    }
    case "error":
      return item.message ? `Error: ${compactText(item.message, 120)}` : "";
    default:
      return "";
  }
}

async function readRequest() {
  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });
  for await (const line of rl) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    return JSON.parse(trimmed);
  }
  throw new Error("No request received.");
}

async function main() {
  const request = await readRequest();
  if (request.action !== "run") {
    throw new Error(`Unsupported bridge action: ${request.action}`);
  }

  const sdkOptions = request.sdk_options || {};
  const codex = new Codex({
    codexPathOverride: sdkOptions.codex_path,
    apiKey: sdkOptions.api_key,
    baseUrl: sdkOptions.base_url,
    config: sdkOptions.config || {},
    env: sdkOptions.env || process.env,
  });

  const thread = codex.startThread(request.thread_options || {});
  const streamed = await thread.runStreamed(request.prompt || "");

  const changedFiles = [];
  const changedSet = new Set();
  const latestItems = new Map();
  let finalResponse = "";
  let usage = null;
  let lastThreadId = null;

  for await (const event of streamed.events) {
    switch (event.type) {
      case "thread.started":
        lastThreadId = event.thread_id || lastThreadId;
        writeEvent({ type: "thread.started", thread_id: lastThreadId });
        break;
      case "turn.completed":
        usage = event.usage || null;
        break;
      case "turn.failed":
        writeEvent({ type: "failed", error: event.error?.message || "Codex turn failed." });
        process.exitCode = 1;
        return;
      case "error":
        writeEvent({ type: "failed", error: event.message || "Codex bridge failed." });
        process.exitCode = 1;
        return;
      case "item.started":
      case "item.updated":
      case "item.completed": {
        const item = event.item;
        if (!item || !item.id) break;
        latestItems.set(item.id, item);

        if (item.type === "agent_message" && typeof item.text === "string" && item.text.trim()) {
          finalResponse = item.text;
        }

        const phase =
          event.type === "item.started"
            ? "started"
            : event.type === "item.completed"
              ? "completed"
              : "updated";
        const summary = summarizeItem(item, phase);
        if (summary) {
          writeEvent({ type: "progress", text: summary });
        }

        if (item.type === "file_change" && Array.isArray(item.changes)) {
          for (const change of item.changes) {
            const path = change?.path;
            if (path && !changedSet.has(path)) {
              changedSet.add(path);
              changedFiles.push(path);
              writeEvent({ type: "file.changed", path, kind: change.kind || "update" });
            }
          }
        }
        break;
      }
      default:
        break;
    }
  }

  writeEvent({
    type: "completed",
    thread_id: thread.id || lastThreadId,
    final_response: finalResponse,
    changed_files: changedFiles,
    usage,
    items: Array.from(latestItems.values()),
  });
}

main().catch((error) => {
  writeEvent({
    type: "failed",
    error: error instanceof Error ? error.message : String(error),
  });
  process.exitCode = 1;
});
