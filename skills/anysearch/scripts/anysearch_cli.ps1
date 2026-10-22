#!/usr/bin/env pwsh
#Requires -Version 5.1

Set-StrictMode -Version Latest

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$ENDPOINT = "https://api.anysearch.com/mcp"
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Load-Env {
    $envPaths = @((Join-Path $SCRIPT_DIR ".env"), (Join-Path (Join-Path $SCRIPT_DIR "..") ".env"))
    foreach ($envPath in $envPaths) {
        if (Test-Path $envPath) {
            Get-Content $envPath -Encoding UTF8 | ForEach-Object {
                $line = $_.Split('#')[0].Trim()
                if ($line -and $line -match '=') {
                    $idx = $line.IndexOf('=')
                    $key = $line.Substring(0, $idx).Trim()
                    $val = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
                    Set-Item -Path "env:$key" -Value $val
                }
            }
        }
    }
}

Load-Env

# BEGIN GENERATED:CONSTANTS
$AVAILABLE_DOMAINS = @(
    "general", "resource", "social_media", "finance", "academic", "legal",
    "health", "business", "security", "ip", "code", "energy",
    "environment", "agriculture", "travel", "film", "gaming"
)
# END GENERATED:CONSTANTS

function Call-Api {
    param(
        [string]$ToolName,
        [hashtable]$Arguments,
        [string]$ApiKey
    )

    $payload = @{
        jsonrpc = "2.0"
        id      = 1
        method  = "tools/call"
        params  = @{
            name      = $ToolName
            arguments = $Arguments
        }
    } | ConvertTo-Json -Depth 10 -Compress

    $headers = @{ "Content-Type" = "application/json; charset=utf-8" }
    if ($ApiKey) {
        $headers["Authorization"] = "Bearer $ApiKey"
    }

    try {
        $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $webReq = [System.Net.HttpWebRequest]::Create($ENDPOINT)
        $webReq.Method = "POST"
        $webReq.ContentType = "application/json; charset=utf-8"
        $webReq.Timeout = 30000
        if ($ApiKey) {
            $webReq.Headers.Add("Authorization", "Bearer $ApiKey")
        }
        $reqStream = $webReq.GetRequestStream()
        $reqStream.Write($bodyBytes, 0, $bodyBytes.Length)
        $reqStream.Close()
        $webResp = $webReq.GetResponse()
        $respStream = $webResp.GetResponseStream()
        $respReader = New-Object System.IO.StreamReader($respStream, [System.Text.Encoding]::UTF8)
        $rawJson = $respReader.ReadToEnd()
        $respReader.Close()
        $webResp.Close()
        $resp = $rawJson | ConvertFrom-Json
    } catch {
        $err = $_.Exception.Message
        Write-Error "Connection Error: Unable to reach the API endpoint. ($err)"
        exit 1
    }

    $hasError = $false
    try { $hasError = ($null -ne $resp.error) } catch { }

    if ($hasError) {
        $errMsg = ""
        try { $errMsg = $resp.error.message } catch { $errMsg = $resp.error | ConvertTo-Json -Depth 5 }
        Write-Error "API Error: $errMsg"
        exit 1
    }

    $result = $null
    try { $result = $resp.result } catch { $result = $resp }

    $toolFailed = $false
    try { $toolFailed = ($result.isError -eq $true) } catch { }
    if ($toolFailed) {
        $errMsg = ($result.content | Where-Object { $_.type -eq "text" } | ForEach-Object { $_.text }) -join "`n"
        if (-not $errMsg) { $errMsg = "MCP tool failed" }
        Write-Error "Tool Error: $errMsg"
        exit 1
    }

    if ($result -and $result.content) {
        foreach ($item in $result.content) {
            if ($item.type -eq "text") {
                return $item.text
            }
        }
    }
    return ($result | ConvertTo-Json -Depth 10)
}

function Parse-JsonList {
    param([string]$Value)
    try {
        $parsed = $Value | ConvertFrom-Json
        if ($parsed -is [array]) { return @($parsed) }
        return @($parsed)
    } catch {
        return @($Value -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
}

function Invoke-Search {
    param([hashtable]$Opts)

    $arguments = @{ query = $Opts.Query }

    if ($Opts.Domain) {
        $arguments["domain"] = $Opts.Domain
        if ($Opts.SubDomain) { $arguments["sub_domain"] = $Opts.SubDomain }
        if ($Opts.SubDomainParams) {
            try {
                $arguments["sub_domain_params"] = $Opts.SubDomainParams | ConvertFrom-Json -AsHashtable
            } catch {
                Write-Error "Error: --sub_domain_params must be valid JSON"
                exit 1
            }
        }
    }

    if ($Opts.MaxResults -ne $null) {
        $arguments["max_results"] = [Math]::Min($Opts.MaxResults, 10)
    }

    $result = Call-Api -ToolName "search" -Arguments $arguments -ApiKey $Opts.ApiKey
    Write-Output $result
}

function Invoke-ListDomains {
    param([hashtable]$Opts)

    $arguments = @{}

    if ($Opts.Domains) {
        $arguments["domains"] = @(Parse-JsonList $Opts.Domains)
    } elseif ($Opts.Domain) {
        $arguments["domain"] = $Opts.Domain
    } else {
        Write-Error "Error: provide --domain or --domains"
        exit 1
    }

    $result = Call-Api -ToolName "get_sub_domains" -Arguments $arguments -ApiKey $Opts.ApiKey
    Write-Output $result
}

function Invoke-Extract {
    param([hashtable]$Opts)

    if (-not $Opts.Url) {
        Write-Error "Error: url is required"
        exit 1
    }

    $arguments = @{ url = $Opts.Url }
    $result = Call-Api -ToolName "extract" -Arguments $arguments -ApiKey $Opts.ApiKey
    Write-Output $result
}

function Repair-Json {
    param([string]$Raw)

    $Raw = $Raw.Trim()
    if ($Raw.StartsWith('{') -and -not $Raw.StartsWith('[')) {
        $Raw = "[$Raw]"
    }
    if ($Raw.StartsWith('[')) {
        $inner = $Raw.Substring(1, $Raw.Length - 2).Trim()
        if (-not $inner) { return @() }
        $items = Split-JsonItems $inner
        $queries = @()
        foreach ($item in $items) {
            $item = $item.Trim().Trim(',')
            if (-not $item) { continue }
            if ($item.StartsWith('{')) {
                $queries += Repair-JsonObject $item
            } else {
                $queries += @{ query = $item.Trim().Trim("'").Trim('"') }
            }
        }
        return $queries
    }
    return @(@{ query = $Raw.Trim().Trim("'").Trim('"') })
}

function Split-JsonItems {
    param([string]$S)

    $depth = 0
    $current = ""
    $items = @()

    foreach ($ch in $S.ToCharArray()) {
        if ($ch -eq '{') { $depth++ }
        elseif ($ch -eq '}') { $depth-- }

        if ($ch -eq ',' -and $depth -eq 0) {
            $items += $current
            $current = ""
        } else {
            $current += $ch
        }
    }
    if ($current) {
        $tail = $current.Trim()
        if ($tail) { $items += $tail }
    }
    return ,$items
}

function Repair-JsonObject {
    param([string]$S)

    $inner = $S.Trim()
    if ($inner.StartsWith('{')) { $inner = $inner.Substring(1) }
    if ($inner.EndsWith('}')) { $inner = $inner.Substring(0, $inner.Length - 1) }
    $inner = $inner.Trim()
    if (-not $inner) { return @{} }

    $pairs = Split-JsonItems $inner
    $result = @{}

    foreach ($pair in $pairs) {
        $p = $pair.Trim().Trim(',')
        if (-not $p -or $p -notmatch ':') { continue }
        $colon = $p.IndexOf(':')
        $key = $p.Substring(0, $colon).Trim().Trim('"').Trim("'")
        $val = $p.Substring($colon + 1).Trim()

        if ($val.StartsWith('{')) {
            try { $result[$key] = $val | ConvertFrom-Json -AsHashtable }
            catch { $result[$key] = Repair-JsonObject $val }
        } elseif ($val.StartsWith('[')) {
            try { $result[$key] = @($val | ConvertFrom-Json) }
            catch { $result[$key] = @($val.Trim('[]') -split ',') }
        } elseif ($val -eq 'true') {
            $result[$key] = $true
        } elseif ($val -eq 'false') {
            $result[$key] = $false
        } elseif ($val -eq 'null') {
            $result[$key] = $null
        } else {
            try { $result[$key] = $val | ConvertFrom-Json }
            catch { $result[$key] = $val.Trim('"').Trim("'") }
        }
    }
    return $result
}

function Invoke-BatchSearch {
    param([hashtable]$Opts)

    $queries = $null

    if ($Opts.QueryItems -and $Opts.QueryItems.Count -gt 0) {
        if ($Opts.QueryItems.Count -gt 5) {
            Write-Error "Error: batch_search supports a maximum of 5 queries"
            exit 1
        }
        $queries = @($Opts.QueryItems | ForEach-Object { @{ query = $_ } })
    } elseif ($Opts.Queries) {
        $raw = $Opts.Queries
        if ($raw.StartsWith('@')) {
            $fpath = $raw.Substring(1)
            if (-not (Test-Path $fpath)) {
                Write-Error "Error: file not found: $fpath"
                exit 1
            }
            $raw = Get-Content $fpath -Raw -Encoding UTF8
        }
        try {
            $parsed = $raw | ConvertFrom-Json
            if ($parsed -is [array]) {
                $queries = @($parsed)
            } else {
                $queries = @($parsed)
            }
        } catch {
            $queries = Repair-Json $raw
        }
    } else {
        Write-Error "Error: provide --queries or --query"
        exit 1
    }

    $qcount = 0
    if ($queries) { $qcount = @($queries).Count }

    if ($qcount -lt 1) {
        Write-Error "Error: queries must contain at least 1 item"
        exit 1
    }
    if ($qcount -gt 5) {
        Write-Error "Error: batch_search supports a maximum of 5 queries"
        exit 1
    }

    $arguments = @{ queries = @($queries) }
    $result = Call-Api -ToolName "batch_search" -Arguments $arguments -ApiKey $Opts.ApiKey
    Write-Output $result
}

# BEGIN GENERATED:DOC_SPEC
function Render-Doc {
    $shared = Join-Path (Split-Path -Parent $MyInvocation.ScriptName) "shared"
    $tpl = Get-Content (Join-Path $shared "doc_spec.md") -Raw -Encoding UTF8
    $c = Get-Content (Join-Path $shared "constants.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $tpl = $tpl.Replace("{{LANG_NAME}}", "PowerShell")
    $tpl = $tpl.Replace("{{LANG_CODEBLOCK}}", "powershell")
    $tpl = $tpl.Replace("{{LANG_INVOKE}}", "powershell -ExecutionPolicy Bypass -File scripts/anysearch_cli.ps1")
    $tpl = $tpl.Replace("{{DOMAINS_SPACE}}", ($c.available_domains -join " "))
    return $tpl
}
# END GENERATED:DOC_SPEC

function Show-Doc {
    Write-Output (Render-Doc)
}

function Show-Usage {
    Show-Doc
}

$apiKey = if ($env:ANYSEARCH_API_KEY) { $env:ANYSEARCH_API_KEY } else { "" }

if ($args.Count -eq 0) {
    Show-Usage
    exit 0
}

$command = $args[0]
if ($args.Count -gt 1) {
    $rest = [array]$args[1..($args.Count - 1)]
} else {
    $rest = [array]@()
}

switch ($command) {
    "-h" { Show-Usage; exit 0 }
    "--help" { Show-Usage; exit 0 }
    "help" { Show-Usage; exit 0 }
}

switch ($command) {
    "search" {
        $query = ""
        $domain = ""
        $subDomain = ""
        $subDomainParams = ""
        $maxResults = $null

        $i = 0
        $positional = @()
        while ($i -lt $rest.Count) {
            if ($rest[$i] -match '^-') { break }
            $positional += $rest[$i]
            $i++
        }
        $query = $positional -join ' '

        while ($i -lt $rest.Count) {
            switch ($rest[$i]) {
                "--domain" { $domain = $rest[$i+1]; $i += 2 }
                "-d"       { $domain = $rest[$i+1]; $i += 2 }
                "--sub_domain" { $subDomain = $rest[$i+1]; $i += 2 }
                "-s"       { $subDomain = $rest[$i+1]; $i += 2 }
                "--sub_domain_params" { $subDomainParams = $rest[$i+1]; $i += 2 }
                "--max_results" { $maxResults = [int]$rest[$i+1]; $i += 2 }
                "-m"       { $maxResults = [int]$rest[$i+1]; $i += 2 }
                "--api_key" { $apiKey = $rest[$i+1]; $i += 2 }
                default    { Write-Error "Unknown flag: $($rest[$i])"; exit 1 }
            }
        }

        if (-not $query) {
            Write-Error "Error: query is required"
            exit 1
        }

        Invoke-Search @{
            Query             = $query
            Domain            = $domain
            SubDomain         = $subDomain
            SubDomainParams   = $subDomainParams
            MaxResults        = $maxResults
            ApiKey            = $apiKey
        }
    }

    "get_sub_domains" {
        $domain = ""
        $domains = ""

        $i = 0
        while ($i -lt $rest.Count) {
            switch ($rest[$i]) {
                "--domain"  { $domain = $rest[$i+1]; $i += 2 }
                "--domains" { $domains = $rest[$i+1]; $i += 2 }
                "--api_key" { $apiKey = $rest[$i+1]; $i += 2 }
                default     { Write-Error "Unknown flag: $($rest[$i])"; exit 1 }
            }
        }

        Invoke-ListDomains @{
            Domain = $domain
            Domains = $domains
            ApiKey  = $apiKey
        }
    }

    "extract" {
        $url = ""
        $positional = @()
        $i = 0

        while ($i -lt $rest.Count) {
            if ($rest[$i] -match '^-') { break }
            $positional += $rest[$i]
            $i++
        }
        $url = $positional -join ' '

        while ($i -lt $rest.Count) {
            switch ($rest[$i]) {
                "--url" { $url = $rest[$i+1]; $i += 2 }
                "-u"    { $url = $rest[$i+1]; $i += 2 }
                "--api_key" { $apiKey = $rest[$i+1]; $i += 2 }
                default { Write-Error "Unknown flag: $($rest[$i])"; exit 1 }
            }
        }

        Invoke-Extract @{ Url = $url; ApiKey = $apiKey }
    }

    "batch_search" {
        $queryItems = [System.Collections.Generic.List[string]]::new()
        $queries = $null
        $positional = $null
        $i = 0

        while ($i -lt $rest.Count) {
            switch ($rest[$i]) {
                "--queries" { $queries = $rest[$i+1]; $i += 2 }
                "-q"        { $queries = $rest[$i+1]; $i += 2 }
                "--query"   { $queryItems.Add($rest[$i+1]); $i += 2 }
                "--api_key" { $apiKey = $rest[$i+1]; $i += 2 }
                default     {
                    if (-not $positional) { $positional = $rest[$i] }
                    else { Write-Error "Unknown argument: $($rest[$i])"; exit 1 }
                    $i++
                }
            }
        }

        if ($positional -and -not $queries) { $queries = $positional }

        Invoke-BatchSearch @{
            Queries    = $queries
            QueryItems = $queryItems
            ApiKey     = $apiKey
        }
    }

    "doc" {
        Show-Doc
    }

    default {
        Write-Error "Unknown command: $command"
        Show-Usage
        exit 1
    }
}
