/**
 * QuickSearch Google Apps Script Proxy
 *
 * Deploys at: https://script.google.com/macros/s/SCRIPT_ID/exec
 *
 * To the school's FortiGate firewall, this looks like a request to
 * script.google.com — a legitimate Google product categorized as
 * "Search Engines/Portals" by FortiGuard, which is allowed on school
 * networks. The SNI says google.com, the TLS cert is Google's real cert.
 *
 * The script receives GET /exec?q=<encrypted>&p=<password> and forwards
 * it to the Render backend, returning the response.
 *
 * Deploy:
 *   1. Go to https://script.google.com (sign in with any Google account)
 *   2. Create a new project
 *   3. Delete the default code, paste this entire file
 *   4. Click Deploy > New deployment
 *   5. Select type: Web app
 *   6. Description: "QuickSearch"
 *   7. Execute as: Me
 *   8. Who has access: Anyone
 *   9. Click Deploy, authorize when prompted
 *  10. Copy the URL: https://script.google.com/macros/s/XXXXX/exec
 *  11. On your Mac: export AISEARCH_URL=https://script.google.com/macros/s/XXXXX/exec
 */

var BACKEND_URL = "https://quicksearch-bw7h.onrender.com";
var SITE_PASSWORD = "tomy"; // must match the server's SITE_PASSWORD env var

function doGet(e) {
  var params = e.parameter;

  // No query param — return a simple page (like visiting the site)
  if (!params.q) {
    return ContentService.createTextOutput("QuickSearch is running.");
  }

  // Check password if one is set
  if (SITE_PASSWORD && params.p !== SITE_PASSWORD) {
    return ContentService
      .createTextOutput(JSON.stringify({ results: [], page: 0, done: true, error: "unauthorized" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  // Forward the request to the Render backend
  // Google Apps Script's UrlFetchApp has a 6-minute timeout
  var backendUrl = BACKEND_URL + "/search?q=" + encodeURIComponent(params.q);
  if (params.p) {
    backendUrl += "&p=" + encodeURIComponent(params.p);
  }

  try {
    var response = UrlFetchApp.fetch(backendUrl, {
      method: "get",
      followRedirects: true,
      muteHttpExceptions: true,
      validateHttpsCertificates: true,
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
      },
    });

    var code = response.getResponseCode();
    var text = response.getContentText();

    if (code !== 200) {
      return ContentService
        .createTextOutput(JSON.stringify({ results: [], page: 0, done: true, error: "HTTP " + code + ": " + text.substring(0, 500) }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // Return the raw NDJSON response from the backend
    // The client will parse the newline-delimited JSON frames
    return ContentService
      .createTextOutput(text)
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ results: [], page: 0, done: true, error: "proxy error: " + err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
