#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const https = require("https");

process.stdout.setDefaultEncoding && process.stdout.setDefaultEncoding("utf-8");

const ENDPOINT = "https://api.anysearch.com/mcp";

// BEGIN GENERATED:CONSTANTS
const AVAILABLE_DOMAINS = [
  "general","resource","social_media","finance","academic","legal",
  "health","business","security","ip","code","energy",
  "environment","agriculture","travel","film","gaming",
];
// END GENERATED:CONSTANTS

function loadEnv() {
  const envPaths = [path.join(__dirname, ".env"), path.join(__dirname, "..", ".env")];
  for (const envPath of envPaths) {
    if (fs.existsSync(envPath)) {
      const lines = fs.readFileSync(envPath, "utf-8").split(/\r?\n/);
      for (const raw of lines) {
        const line = raw.replace(/#.*$/, "").trim();
        if (!line || line.indexOf("=") === -1) continue;
        const idx = line.indexOf("=");
        const key = line.substring(0, idx).trim();
        let val = line.substring(idx + 1).trim().replace(/^["']|["']$/g, "");
        process.env[key] = val;
      }
    }
  }
}

loadEnv();

function httpRequest(url, payload, apikey) {
  const body = JSON.stringify(payload);
  const urlObj = new URL(url);
  const options = {
    hostname: urlObj.hostname,
    path: urlObj.pathname,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body),
    },
  };
  if (apikey) {
    options.headers["Authorization"] = `Bearer ${apikey}`;
  }

  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          const json = JSON.parse(data);
          if (res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode}: ${JSON.stringify(json)}`));
            return;
          }
          if (json.error) {
            reject(new Error(json.error.message || JSON.stringify(json.error)));
            return;
          }
          const content = json.result && json.result.content;
          if (json.result && json.result.isError) {
            const text = Array.isArray(content)
              ? content.filter((item) => item.type === "text").map((item) => item.text).join("\n")
              : "";
            reject(new Error(`Tool Error: ${text || "MCP tool failed"}`));
            return;
          }
          if (Array.isArray(content)) {
            const textItem = content.find((c) => c.type === "text");
            if (textItem) {
              resolve(textItem.text);
              return;
            }
          }
          resolve(JSON.stringify(json.result || json, null, 2));
        } catch (e) {
          reject(new Error(`Invalid JSON response: ${data.slice(0, 500)}`));
        }
      });
    });
    req.setTimeout(30000, () => {
      req.destroy();
      reject(new Error("Timeout: The API request timed out."));
    });
    req.on("error", (e) => reject(new Error(`Connection Error: ${e.message}`)));
    req.write(body);
    req.end();
  });
}

async function callApi(toolName, args, apikey) {
  const payload = {
    jsonrpc: "2.0",
    id: 1,
    method: "tools/call",
    params: { name: toolName, arguments: args },
  };
  try {
    return await httpRequest(ENDPOINT, payload, apikey);
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }
}

function parseJsonList(value) {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [parsed];
  } catch (_) {
    return value.split(",").map((s) => s.trim()).filter(Boolean);
  }
}

async function cmdSearch(opts) {
  const args = { query: opts.query };

  if (opts.domain) {
    args.domain = opts.domain;
    if (opts.subDomain) args.sub_domain = opts.subDomain;
    if (opts.subDomainParams) {
      try {
        args.sub_domain_params = JSON.parse(opts.subDomainParams);
      } catch (_) {
        console.error("Error: --sub_domain_params must be valid JSON");
        process.exit(1);
      }
    }
  }

  if (opts.maxResults !== undefined) args.max_results = Math.min(opts.maxResults, 10);

  const result = await callApi("search", args, opts.apiKey);
  console.log(result);
}

async function cmdListDomains(opts) {
  let args;
  if (opts.domains) {
    args = { domains: parseJsonList(opts.domains) };
  } else if (opts.domain) {
    args = { domain: opts.domain };
  } else {
    console.error("Error: provide --domain or --domains");
    process.exit(1);
  }

  const result = await callApi("get_sub_domains", args, opts.apiKey);
  console.log(result);
}

async function cmdExtract(opts) {
  const url = opts.url;
  if (!url) {
    console.error("Error: url is required");
    process.exit(1);
  }
  const result = await callApi("extract", { url }, opts.apiKey);
  console.log(result);
}

function repairJson(raw) {
  raw = raw.trim();
  if (raw.startsWith("{") && !raw.startsWith("[")) raw = "[" + raw + "]";
  if (raw.startsWith("[")) {
    const content = raw.slice(1, -1).trim();
    if (!content) return [];
    const items = splitJsonItems(content);
    return items.map((item) => {
      item = item.trim().replace(/^,|,$/g, "");
      if (!item) return null;
      if (item.startsWith("{")) return repairJsonObject(item);
      return { query: item.trim().replace(/^['"]|['"]$/g, "") };
    }).filter(Boolean);
  }
  return [{ query: raw.trim().replace(/^['"]|['"]$/g, "") }];
}

function splitJsonItems(s) {
  let depth = 0;
  let current = "";
  const items = [];
  for (const ch of s) {
    if (ch === "{") depth++;
    else if (ch === "}") depth--;
    if (ch === "," && depth === 0) {
      items.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  if (current.trim()) items.push(current);
  return items;
}

function repairJsonObject(s) {
  const inner = s.trim().replace(/^{|}$/g, "").trim();
  if (!inner) return {};
  const pairs = splitJsonItems(inner);
  const result = {};
  for (const pair of pairs) {
    const p = pair.trim().replace(/^,|,$/g, "");
    if (!p || p.indexOf(":") === -1) continue;
    const colon = p.indexOf(":");
    const key = p.substring(0, colon).trim().replace(/^['"]|['"]$/g, "");
    let val = p.substring(colon + 1).trim();
    if (val.startsWith("{")) {
      try { result[key] = JSON.parse(val); } catch (_) { result[key] = repairJsonObject(val); }
    } else if (val.startsWith("[")) {
      try { result[key] = JSON.parse(val); } catch (_) { result[key] = val.slice(1, -1).split(","); }
    } else if (val === "true") {
      result[key] = true;
    } else if (val === "false") {
      result[key] = false;
    } else if (val === "null") {
      result[key] = null;
    } else {
      try { result[key] = JSON.parse(val); } catch (_) { result[key] = val.replace(/^['"]|['"]$/g, ""); }
    }
  }
  return result;
}

async function cmdBatchSearch(opts) {
  let queries;

  if (opts.queryItems && opts.queryItems.length > 0) {
    if (opts.queryItems.length > 5) {
      console.error("Error: batch_search supports a maximum of 5 queries");
      process.exit(1);
    }
    queries = opts.queryItems.map((q) => ({ query: q }));
  } else if (opts.queries) {
    let raw = opts.queries;
    if (raw.startsWith("@")) {
      const fpath = raw.substring(1);
      if (!fs.existsSync(fpath)) {
        console.error(`Error: file not found: ${fpath}`);
        process.exit(1);
      }
      raw = fs.readFileSync(fpath, "utf-8");
    }
    try {
      const parsed = JSON.parse(raw);
      queries = Array.isArray(parsed) ? parsed : [parsed];
    } catch (_) {
      queries = repairJson(raw);
    }
  } else {
    console.error("Error: provide --queries or --query");
    process.exit(1);
  }

  if (queries.length < 1) {
    console.error("Error: queries must contain at least 1 item");
    process.exit(1);
  }
  if (queries.length > 5) {
    console.error("Error: batch_search supports a maximum of 5 queries");
    process.exit(1);
  }

  const result = await callApi("batch_search", { queries }, opts.apiKey);
  console.log(result);
}

// BEGIN GENERATED:DOC_SPEC
function renderDoc() {
  const shared = path.join(__dirname, "shared");
  let tpl = fs.readFileSync(path.join(shared, "doc_spec.md"), "utf-8");
  const c = JSON.parse(fs.readFileSync(path.join(shared, "constants.json"), "utf-8"));
  tpl = tpl.replace(/\{\{LANG_NAME\}\}/g, "Node.js");
  tpl = tpl.replace(/\{\{LANG_CODEBLOCK\}\}/g, "");
  tpl = tpl.replace(/\{\{LANG_INVOKE\}\}/g, "node scripts/anysearch_cli.js");
  tpl = tpl.replace(/\{\{DOMAINS_SPACE\}\}/g, c.available_domains.join(" "));
  return tpl;
}
// END GENERATED:DOC_SPEC

function cmdDoc() {
  console.log(renderDoc());
}

function usage() {
  cmdDoc();
}

function parseArgs(argv) {
  const args = argv.slice(2);
  const command = args[0] || "";
  const rest = args.slice(1);
  const opts = { apiKey: process.env.ANYSEARCH_API_KEY || "" };

  function shiftVal() {
    if (rest.length === 0) {
      console.error(`Error: missing value for ${rest[0] || "option"}`);
      process.exit(1);
    }
    return rest.shift();
  }

  function nextFlag() {
    return rest.length > 0 && rest[0].startsWith("--");
  }

  switch (command) {
    case "search": {
      opts.query = "";
      while (rest.length > 0 && !rest[0].startsWith("-")) {
        opts.query += (opts.query ? " " : "") + rest.shift();
      }
      if (!opts.query && rest.length > 0 && !rest[0].startsWith("-")) {
        opts.query = rest.shift();
      }
      while (rest.length > 0) {
        const flag = rest.shift();
        switch (flag) {
          case "--domain": case "-d": opts.domain = shiftVal(); break;
          case "--sub_domain": case "-s": opts.subDomain = shiftVal(); break;
          case "--sub_domain_params": opts.subDomainParams = shiftVal(); break;
          case "--max_results": case "-m": opts.maxResults = parseInt(shiftVal(), 10); break;
          case "--api_key": opts.apiKey = shiftVal(); break;
          default: console.error(`Unknown flag: ${flag}`); usage(); process.exit(1);
        }
      }
      if (!opts.query) {
        console.error("Error: query is required");
        process.exit(1);
      }
      return { action: "search", opts };
    }

    case "get_sub_domains": {
      while (rest.length > 0) {
        const flag = rest.shift();
        switch (flag) {
          case "--domain": opts.domain = shiftVal(); break;
          case "--domains": opts.domains = shiftVal(); break;
          case "--api_key": opts.apiKey = shiftVal(); break;
          default: console.error(`Unknown flag: ${flag}`); process.exit(1);
        }
      }
      return { action: "listDomains", opts };
    }

    case "extract": {
      opts.url = "";
      while (rest.length > 0 && !rest[0].startsWith("-")) {
        opts.url += (opts.url ? " " : "") + rest.shift();
      }
      while (rest.length > 0) {
        const flag = rest.shift();
        switch (flag) {
          case "--url": case "-u": opts.url = shiftVal(); break;
          case "--api_key": opts.apiKey = shiftVal(); break;
          default: console.error(`Unknown flag: ${flag}`); process.exit(1);
        }
      }
      return { action: "extract", opts };
    }

    case "batch_search": {
      opts.queryItems = [];
      opts.queries = undefined;
      let positional = undefined;
      while (rest.length > 0) {
        const flag = rest.shift();
        switch (flag) {
          case "--queries": case "-q": opts.queries = shiftVal(); break;
          case "--query": opts.queryItems.push(shiftVal()); break;
          case "--api_key": opts.apiKey = shiftVal(); break;
          default:
            if (!positional) positional = flag;
            else { console.error(`Unknown argument: ${flag}`); process.exit(1); }
        }
      }
      if (positional) opts.queries = opts.queries || positional;
      return { action: "batchSearch", opts };
    }

    case "doc":
      return { action: "doc", opts };

    case "-h": case "--help": case "help":
      usage();
      process.exit(0);

    default:
      if (!command) { usage(); process.exit(0); }
      console.error(`Unknown command: ${command}`);
      usage();
      process.exit(1);
  }
}

async function main() {
  const { action, opts } = parseArgs(process.argv);

  switch (action) {
    case "search": await cmdSearch(opts); break;
    case "listDomains": await cmdListDomains(opts); break;
    case "extract": await cmdExtract(opts); break;
    case "batchSearch": await cmdBatchSearch(opts); break;
    case "doc": cmdDoc(); break;
  }
}

main().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
