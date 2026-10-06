// Read-only Search Console API bridge. A completed page walk covers API-visible rows,
// never all queries or pages in the market: Search Console can omit hidden rows.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const GSC_PAGE_SIZE = 25000;

function safeProviderCode(error) {
  const code = String(error?.message || '');
  return /^(?:gsc_http_[1-5]\d\d|oauth_http_[1-5]\d\d|oauth_invalid_grant|refresh_token_expired)$/.test(code)
    ? code : 'provider_error';
}

export async function accessTokenForFile(client, tokenFile, fetchImpl = fetch) {
  const readSaved = () => JSON.parse(fs.readFileSync(tokenFile, 'utf8'));
  const valid = saved => saved.access_token && Number(saved.expiry_date || 0) > Date.now() + 60_000;
  let saved = readSaved();
  if (valid(saved)) return saved.access_token;
  const lockPath = tokenFile + '.lock';
  let lock;
  const deadline = Date.now() + 30000;
  while (!lock) {
    try { lock = fs.openSync(lockPath,'wx',0o600); }
    catch (error) {
      if (error.code !== 'EEXIST') throw new Error('token_lock_failed');
      if (Date.now() >= deadline) throw new Error('token_lock_busy');
      await new Promise(resolve=>setTimeout(resolve,100));
    }
  }
  try {
    saved = readSaved(); // Another process may have refreshed it while we waited.
    if (valid(saved)) return saved.access_token;
    if (!saved.refresh_token) throw new Error('refresh_token_missing');
    if (saved.refresh_token_expiry_date &&
        Number(saved.refresh_token_expiry_date) <= Date.now()) throw new Error('refresh_token_expired');
    const tokenUrl = new URL(client.token_uri || 'https://oauth2.googleapis.com/token');
    if (tokenUrl.protocol !== 'https:' || tokenUrl.hostname !== 'oauth2.googleapis.com') throw new Error('unexpected_token_provider');
    const response = await fetchImpl(tokenUrl, {method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},
      signal:AbortSignal.timeout(30000),
      body:new URLSearchParams({client_id:client.client_id,client_secret:client.client_secret,
        refresh_token:saved.refresh_token,grant_type:'refresh_token'})});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload?.error === 'invalid_grant' ? 'oauth_invalid_grant' : `oauth_http_${response.status}`);
    if (!payload.access_token || !Number(payload.expires_in)) throw new Error('oauth_invalid_response');
    const updated = {...saved, access_token:payload.access_token,
      expiry_date:Date.now()+Number(payload.expires_in)*1000,
      refresh_token:payload.refresh_token || saved.refresh_token};
    const pending = tokenFile + '.pending-' + process.pid;
    try {
      fs.writeFileSync(pending,JSON.stringify(updated,null,2),{mode:0o600});
      fs.renameSync(pending,tokenFile);
    } catch {
      throw new Error('token_write_failed');
    } finally {
      try { fs.unlinkSync(pending); } catch {}
    }
    return updated.access_token;
  } finally {
    fs.closeSync(lock);
    fs.unlinkSync(lockPath);
  }
}

export async function collectSearchAnalytics(requestPage, {pageSize = GSC_PAGE_SIZE, previewRows = null} = {}) {
  if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > GSC_PAGE_SIZE) throw new Error('invalid_page_size');
  if (previewRows !== null && (!Number.isInteger(previewRows) || previewRows < 1)) throw new Error('invalid_preview_rows');
  const rows = [];
  const seen = new Set();
  let startRow = 0, pagesFetched = 0, apiRequestsAttempted = 0, reason = null, apiExhausted = false;
  let providerErrorCode = null;
  while (true) {
    const rowLimit = previewRows === null ? pageSize : Math.min(pageSize, previewRows - rows.length);
    if (rowLimit < 1) { reason = 'preview_limit'; break; }
    let payload;
    apiRequestsAttempted++;
    try { payload = await requestPage({startRow, rowLimit}); }
    catch (error) {
      if (pagesFetched === 0) throw new Error(safeProviderCode(error));
      reason = 'provider_error';
      providerErrorCode = safeProviderCode(error);
      break;
    }
    const page = payload?.rows || [];
    if (!Array.isArray(page) || page.length > rowLimit) { reason = 'invalid_provider_page'; break; }
    pagesFetched++;
    if (page.length === 0) { apiExhausted = true; break; }
    let added = 0;
    for (const row of page) {
      const key = JSON.stringify(row.keys || []);
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({keys:row.keys, clicks:row.clicks, impressions:row.impressions,
                 ctr:row.ctr, position:row.position});
      added++;
    }
    if (added === 0) { reason = 'repeated_page'; break; }
    if (added !== page.length) { reason = 'duplicate_rows'; break; }
    startRow += page.length;
    if (page.length < rowLimit) { apiExhausted = true; break; }
    if (previewRows !== null && rows.length >= previewRows) { reason = 'preview_limit'; break; }
  }
  return {rows, pages_fetched:pagesFetched, api_requests_attempted:apiRequestsAttempted,
    api_exhausted:apiExhausted,
    truncated:reason === 'preview_limit', incomplete:true,
    coverage:{scope:'gsc_api_visible_rows_only', provider_hidden_rows_possible:true,
              absent_row_is_not_zero:true,
              rows_retrieved:rows.length, stop_reason:reason || 'api_exhausted',
              ...(providerErrorCode ? {provider_error_code:providerErrorCode} : {})}};
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i], value = argv[i + 1];
    if (!key?.startsWith('--') || value === undefined) throw new Error('invalid_arguments');
    args[key.slice(2)] = value;
  }
  return args;
}

export function siteScopeMatches(property, requested) {
  try {
    const target = new URL(requested);
    if (!['https:', 'http:'].includes(target.protocol)) return false;
    if (property.startsWith('sc-domain:')) {
      const domain = property.slice('sc-domain:'.length).toLowerCase();
      return target.hostname.toLowerCase() === domain || target.hostname.toLowerCase().endsWith('.' + domain);
    }
    const scope = new URL(property);
    return target.protocol === scope.protocol && target.hostname.toLowerCase() === scope.hostname.toLowerCase() &&
      target.port === scope.port && target.pathname.startsWith(scope.pathname);
  } catch { return false; }
}

async function main() {
  if (process.argv.includes('--help')) {
    console.log('Read-only GSC: --mode sites|queries --site PROPERTY --client-file PATH --token-file PATH --output PATH. Client/token paths may also come from GSC_OAUTH_CLIENT_FILE and GSC_TOKEN_FILE after loading the authorized connection. Queries require --date-from YYYY-MM-DD --date-to YYYY-MM-DD --dimensions total|page|query. Optional --page-size (technical page size), --preview-rows (explicit partial preview), --page-contains, --query-contains, --segment-label. Pagination exhaustion does not prove full site coverage.');
    return;
  }
  const args = parseArgs(process.argv.slice(2));
  const mode = args.mode;
  if (!['sites', 'queries'].includes(mode)) throw new Error('invalid_mode');
  if (!args.site) throw new Error('site_required');
  const clientFile = args['client-file'] || process.env.GSC_OAUTH_CLIENT_FILE;
  const tokenFile = args['token-file'] || process.env.GSC_TOKEN_FILE;
  if (!clientFile || !tokenFile) throw new Error('credential_paths_required');
  if (mode === 'queries' && (!args['date-from'] || !args['date-to'])) throw new Error('query_scope_required');
  const pageSize = Number(args['page-size'] || GSC_PAGE_SIZE);
  const previewRows = args['preview-rows'] || args['max-rows'] || null;
  const preview = previewRows === null ? null : Number(previewRows);
  const dimensions = args.dimensions === 'total' ? [] : String(args.dimensions || 'page').split(',').map(x=>x.trim());
  if (mode === 'queries' && dimensions.some(x=>!['page','query','date','device','country'].includes(x))) throw new Error('invalid_dimensions');
  const client = JSON.parse(fs.readFileSync(clientFile, 'utf8')).installed;
  if (!client?.client_id || !client?.client_secret) throw new Error('invalid_client_file');
  const access = await accessTokenForFile(client,tokenFile);
  const headers = {authorization:`Bearer ${access}`};
  let result;
  if (mode === 'sites') {
    const response = await fetch('https://www.googleapis.com/webmasters/v3/sites', {headers,signal:AbortSignal.timeout(30000)});
    if (!response.ok) throw new Error(`gsc_http_${response.status}`);
    const payload = await response.json();
    const sites = (payload.siteEntry || []).filter(x=>siteScopeMatches(x.siteUrl, args.site))
      .map(x=>({siteUrl:x.siteUrl,permissionLevel:x.permissionLevel}));
    if (!sites.length) throw new Error('gsc_site_not_listed');
    result = {source:'Google Search Console',mode,requested_site:args.site,sites};
  } else {
    const endpoint = `https://www.googleapis.com/webmasters/v3/sites/${encodeURIComponent(args.site)}/searchAnalytics/query`;
    const filters = [];
    if (args['page-contains']) filters.push({dimension:'page',operator:'contains',expression:args['page-contains']});
    if (args['query-contains']) filters.push({dimension:'query',operator:'contains',expression:args['query-contains']});
    const requestPage = async ({startRow,rowLimit}) => {
      const body = {startDate:args['date-from'],endDate:args['date-to'],dimensions,
        type:'web',dataState:'final',rowLimit,startRow};
      if (filters.length) body.dimensionFilterGroups = [{groupType:'and',filters}];
      const response = await fetch(endpoint,{method:'POST',headers:{...headers,'content-type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
      if (!response.ok) throw new Error(`gsc_http_${response.status}`);
      return response.json();
    };
    const collected = await collectSearchAnalytics(requestPage,{pageSize,previewRows:preview});
    result = {source:'Google Search Console',mode,site:args.site,date_from:args['date-from'],
      date_to:args['date-to'],data_state:'final',dimensions,filters,
      segment_label:args['segment-label'] || null,segment_intent_verified:false,...collected};
  }
  if (args.output) { fs.mkdirSync(path.dirname(args.output),{recursive:true}); fs.writeFileSync(args.output,JSON.stringify(result,null,2),'utf8'); }
  const status = mode === 'queries' && result.coverage.stop_reason !== 'api_exhausted' ? 'partial' : 'ok';
  console.log(JSON.stringify({system:'GSC',mode,status,rows:mode==='sites'?result.sites.length:result.rows.length}));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch(error=>{
    const code = /^[A-Za-z0-9_:-]{1,100}$/.test(String(error.message)) ? error.message : 'bridge_failed';
    console.log(JSON.stringify({system:'GSC',status:'error',error_code:code}));
    process.exitCode=1;
  });
}
