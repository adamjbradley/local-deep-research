/**
 * Structured Results Renderer
 *
 * Renders structured research data as an interactive table with
 * confidence indicators, source links, and export buttons.
 * Integrates with the existing results.js lifecycle.
 */
(function () {
  "use strict";

  /**
   * Check if the response contains structured research data
   * and render it if so. Returns true if handled, false otherwise.
   */
  function tryRenderStructured(responseData, container) {
    const sd = responseData.structured_data;
    if (!sd || !sd.cells || !sd.cells.length) return false;

    const mode = responseData.metadata && responseData.metadata.mode;
    if (mode !== "structured") return false;

    renderStructuredData(sd, container);
    showStructuredExportButtons(responseData);

    // Initialize refinement panel
    if (window.structuredRefinement) {
      window.structuredRefinement.init(responseData);
    }

    return true;
  }

  /**
   * Render structured data as a results table
   */
  function renderStructuredData(sd, container) {
    const schema = sd.schema || {};
    const cells = sd.cells || [];
    const sources = sd.sources || [];
    const warnings = sd.warnings || [];

    // Build source lookup
    var sourceLookup = {};
    for (var i = 0; i < sources.length; i++) {
      sourceLookup[sources[i].id] = sources[i];
    }

    // Collect dimension names and field names
    var dimNames = [];
    var fieldNames = [];
    var fieldDefs = schema.fields || [];

    for (var ci = 0; ci < cells.length; ci++) {
      var dv = cells[ci].dimension_values || {};
      var keys = Object.keys(dv);
      for (var ki = 0; ki < keys.length; ki++) {
        if (dimNames.indexOf(keys[ki]) === -1) dimNames.push(keys[ki]);
      }
    }

    for (var fi = 0; fi < fieldDefs.length; fi++) {
      if (fieldDefs[fi].name) fieldNames.push(fieldDefs[fi].name);
    }

    // If no field defs, infer from first item
    if (!fieldNames.length && cells.length && cells[0].items && cells[0].items.length) {
      var firstItem = cells[0].items[0];
      var itemKeys = Object.keys(firstItem);
      for (var ik = 0; ik < itemKeys.length; ik++) {
        var k = itemKeys[ik];
        if (["source_ids", "source_count", "confidence", "item_id", "conflicts", "appended_from"].indexOf(k) === -1 && k.charAt(0) !== "_") {
          fieldNames.push(k);
        }
      }
    }

    // Build HTML using DOM
    var wrapper = document.createElement("div");
    wrapper.className = "structured-results";

    // Summary stats
    var totalItems = 0;
    for (var si = 0; si < cells.length; si++) {
      totalItems += (cells[si].items || []).length;
    }

    var summary = document.createElement("div");
    summary.className = "structured-summary";
    summary.style.cssText = "margin-bottom:1.5rem; padding:1rem; background:var(--bg-tertiary); border-radius:8px; display:flex; gap:2rem; flex-wrap:wrap;";

    var stats = [
      { label: "Cells", value: cells.length },
      { label: "Items found", value: totalItems },
      { label: "Sources", value: sources.length },
    ];

    for (var sti = 0; sti < stats.length; sti++) {
      var stat = document.createElement("div");
      var labelEl = document.createElement("span");
      labelEl.style.cssText = "color:var(--text-muted); font-size:0.85rem; display:block;";
      labelEl.textContent = stats[sti].label;
      var valEl = document.createElement("span");
      valEl.style.cssText = "font-size:1.4rem; font-weight:600; color:var(--accent-primary);";
      valEl.textContent = String(stats[sti].value);
      stat.appendChild(labelEl);
      stat.appendChild(valEl);
      summary.appendChild(stat);
    }
    wrapper.appendChild(summary);

    // Section navigation bar
    var nav = document.createElement("nav");
    nav.style.cssText = "display:flex; gap:1rem; flex-wrap:wrap; margin-bottom:1rem; padding:0.5rem 0; border-bottom:1px solid var(--border-color); position:sticky; top:0; background:var(--bg-primary); z-index:10;";
    var sections = [
      { id: "sr-data-table", label: "Data Table" },
      { id: "sr-sources", label: "Sources" },
      { id: "sr-summaries", label: "Summaries" },
    ];
    for (var ni = 0; ni < sections.length; ni++) {
      var navLink = document.createElement("a");
      navLink.href = "#" + sections[ni].id;
      navLink.textContent = sections[ni].label;
      navLink.style.cssText = "color:var(--accent-primary); text-decoration:none; font-size:0.85rem; font-weight:500; padding:0.25rem 0.5rem; border-radius:4px;";
      navLink.addEventListener("mouseover", function() { this.style.background = "var(--bg-tertiary)"; });
      navLink.addEventListener("mouseout", function() { this.style.background = ""; });
      nav.appendChild(navLink);
    }
    wrapper.appendChild(nav);

    // Warnings
    if (warnings.length) {
      var warnDiv = document.createElement("div");
      warnDiv.className = "ldr-alert ldr-alert-warning";
      warnDiv.style.marginBottom = "1rem";
      for (var wi = 0; wi < warnings.length; wi++) {
        var wp = document.createElement("p");
        wp.textContent = warnings[wi];
        warnDiv.appendChild(wp);
      }
      wrapper.appendChild(warnDiv);
    }

    // Data table (collapsible)
    var tableDetails = document.createElement("details");
    tableDetails.id = "sr-data-table";
    tableDetails.open = true;
    tableDetails.style.cssText = "margin-bottom:1.5rem;";

    var tableSummary = document.createElement("summary");
    tableSummary.style.cssText = "cursor:pointer; font-weight:600; font-size:1.1rem; color:var(--text-primary); padding:0.5rem 0; margin-bottom:0.5rem;";
    tableSummary.textContent = "Data Table (" + totalItems + " items across " + cells.length + " cells)";
    tableDetails.appendChild(tableSummary);

    var table = document.createElement("table");
    table.style.cssText = "width:100%; border-collapse:collapse;";

    // Header
    var thead = document.createElement("thead");
    var headerRow = document.createElement("tr");
    var allCols = dimNames.concat(fieldNames).concat(["Confidence", "Sources"]);

    for (var hi = 0; hi < allCols.length; hi++) {
      var th = document.createElement("th");
      th.textContent = allCols[hi];
      th.style.cssText = "background:var(--bg-tertiary); text-align:left; padding:0.75rem; border-bottom:2px solid var(--border-color); color:var(--accent-tertiary); font-size:0.85rem; text-transform:uppercase; letter-spacing:0.05em;";
      headerRow.appendChild(th);
    }
    thead.appendChild(headerRow);
    table.appendChild(thead);

    // Body
    var tbody = document.createElement("tbody");

    for (var ri = 0; ri < cells.length; ri++) {
      var cell = cells[ri];
      var items = cell.items || [];
      var dimVals = cell.dimension_values || {};

      if (!items.length) {
        // Empty cell row
        var emptyRow = document.createElement("tr");
        for (var di = 0; di < dimNames.length; di++) {
          var dtd = document.createElement("td");
          dtd.textContent = dimVals[dimNames[di]] || "";
          dtd.style.cssText = "padding:0.75rem; border-bottom:1px solid var(--border-color); font-weight:500;";
          emptyRow.appendChild(dtd);
        }
        var emptyTd = document.createElement("td");
        emptyTd.colSpan = fieldNames.length + 2;
        emptyTd.textContent = cell.error || "No items found";
        emptyTd.style.cssText = "padding:0.75rem; border-bottom:1px solid var(--border-color); color:var(--text-muted); font-style:italic;";
        emptyRow.appendChild(emptyTd);
        tbody.appendChild(emptyRow);
        continue;
      }

      for (var ii = 0; ii < items.length; ii++) {
        var item = items[ii];
        var row = document.createElement("tr");

        // Dimension columns (only show on first item of cell)
        for (var dii = 0; dii < dimNames.length; dii++) {
          var dimTd = document.createElement("td");
          if (ii === 0) {
            dimTd.textContent = dimVals[dimNames[dii]] || "";
            dimTd.style.fontWeight = "500";
            if (items.length > 1) dimTd.rowSpan = items.length;
          } else {
            dimTd.style.display = "none";
          }
          dimTd.style.cssText += "padding:0.75rem; border-bottom:1px solid var(--border-color); vertical-align:top;";
          row.appendChild(dimTd);
        }

        // Field columns
        for (var fii = 0; fii < fieldNames.length; fii++) {
          var fTd = document.createElement("td");
          var val = item[fieldNames[fii]] || "";
          var rawKey = "_" + fieldNames[fii] + "_raw";
          if (item[rawKey]) {
            fTd.textContent = val + " ";
            var rawSpan = document.createElement("span");
            rawSpan.textContent = "(raw: " + item[rawKey] + ")";
            rawSpan.style.cssText = "color:var(--text-muted); font-size:0.8rem;";
            fTd.appendChild(rawSpan);
          } else {
            fTd.textContent = val;
          }
          fTd.style.cssText = "padding:0.75rem; border-bottom:1px solid var(--border-color); color:var(--text-secondary);";
          row.appendChild(fTd);
        }

        // Confidence badge
        var confTd = document.createElement("td");
        var confBadge = document.createElement("span");
        var conf = item.confidence || "unknown";
        var confColors = { high: "#22c55e", medium: "#eab308", low: "#f97316", unverified: "#ef4444", unknown: "#6b7280" };
        confBadge.textContent = conf;
        confBadge.style.cssText = "padding:0.2rem 0.5rem; border-radius:4px; font-size:0.75rem; font-weight:600; color:#fff; background:" + (confColors[conf] || confColors.unknown) + ";";
        confTd.appendChild(confBadge);
        var countSpan = document.createElement("span");
        countSpan.textContent = " (" + (item.source_count || 0) + ")";
        countSpan.style.cssText = "color:var(--text-muted); font-size:0.8rem;";
        confTd.appendChild(countSpan);
        confTd.style.cssText = "padding:0.75rem; border-bottom:1px solid var(--border-color);";
        row.appendChild(confTd);

        // Source links
        var srcTd = document.createElement("td");
        var srcIds = item.source_ids || [];
        for (var si2 = 0; si2 < srcIds.length; si2++) {
          var src = sourceLookup[srcIds[si2]];
          if (src && src.url) {
            var a = document.createElement("a");
            a.href = src.url;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.textContent = srcIds[si2];
            a.title = src.title || src.url;
            a.style.cssText = "color:var(--accent-primary); text-decoration:none; margin-right:0.3rem; font-size:0.8rem;";
            srcTd.appendChild(a);
          } else {
            var span = document.createElement("span");
            span.textContent = srcIds[si2] + " ";
            span.style.cssText = "color:var(--text-muted); font-size:0.8rem;";
            srcTd.appendChild(span);
          }
        }
        srcTd.style.cssText = "padding:0.75rem; border-bottom:1px solid var(--border-color);";
        row.appendChild(srcTd);

        tbody.appendChild(row);
      }
    }

    table.appendChild(tbody);
    tableDetails.appendChild(table);
    wrapper.appendChild(tableDetails);

    // Sources section
    if (sources.length) {
      var srcSection = document.createElement("details");
      srcSection.id = "sr-sources";
      srcSection.style.cssText = "margin-top:1rem;";
      var srcSummary = document.createElement("summary");
      srcSummary.textContent = "Sources (" + sources.length + ")";
      srcSummary.style.cssText = "cursor:pointer; font-weight:600; font-size:1.1rem; color:var(--text-primary); padding:0.5rem 0;";
      srcSection.appendChild(srcSummary);

      var srcList = document.createElement("div");
      srcList.style.cssText = "padding:0.5rem 0;";
      for (var sli = 0; sli < sources.length; sli++) {
        var s = sources[sli];
        var srcItem = document.createElement("div");
        srcItem.style.cssText = "padding:0.4rem 0; border-bottom:1px solid var(--border-color); font-size:0.85rem;";

        var idSpan = document.createElement("span");
        idSpan.textContent = s.id + ": ";
        idSpan.style.cssText = "color:var(--accent-primary); font-weight:500;";
        srcItem.appendChild(idSpan);

        if (s.url) {
          var srcLink = document.createElement("a");
          srcLink.href = s.url;
          srcLink.target = "_blank";
          srcLink.rel = "noopener noreferrer";
          srcLink.textContent = s.title || s.url;
          srcLink.style.color = "var(--text-secondary)";
          srcItem.appendChild(srcLink);
        } else {
          var titleSpan = document.createElement("span");
          titleSpan.textContent = s.title || "(no URL)";
          srcItem.appendChild(titleSpan);
        }

        if (s.content_date) {
          var dateSpan = document.createElement("span");
          dateSpan.textContent = " [" + s.content_date + "]";
          dateSpan.style.cssText = "color:var(--text-muted); font-size:0.8rem;";
          srcItem.appendChild(dateSpan);
        }

        if (s.engine) {
          var engineSpan = document.createElement("span");
          engineSpan.textContent = " via " + s.engine;
          engineSpan.style.cssText = "color:var(--text-muted); font-size:0.75rem;";
          srcItem.appendChild(engineSpan);
        }

        srcList.appendChild(srcItem);
      }
      srcSection.appendChild(srcList);
      wrapper.appendChild(srcSection);
    }

    // Summaries section
    var summariesDiv = document.createElement("div");
    summariesDiv.id = "sr-summaries";
    summariesDiv.style.marginTop = "1.5rem";

    var existingSummaries = sd.summaries || [];
    if (existingSummaries.length) {
      renderSummaries(existingSummaries, summariesDiv);
    }

    // "Generate Summaries" button (on-demand)
    var genBtnRow = document.createElement("div");
    genBtnRow.style.cssText = "margin-top:1rem; display:flex; gap:1rem; align-items:center;";

    var genBtn = document.createElement("button");
    genBtn.className = "btn ldr-btn-outline";
    genBtn.type = "button";
    var genIcon = document.createElement("i");
    genIcon.className = "fas fa-file-alt";
    genIcon.setAttribute("aria-hidden", "true");
    genBtn.appendChild(genIcon);
    genBtn.appendChild(document.createTextNode(
      existingSummaries.length ? " Regenerate Summaries" : " Generate Dimension Summaries"
    ));
    genBtn.addEventListener("click", function () {
      generateSummariesOnDemand(summariesDiv, genBtn);
    });
    genBtnRow.appendChild(genBtn);

    var genStatus = document.createElement("span");
    genStatus.id = "gen-summaries-status";
    genStatus.style.cssText = "color:var(--text-muted); font-size:0.85rem;";
    genBtnRow.appendChild(genStatus);

    wrapper.appendChild(genBtnRow);
    wrapper.appendChild(summariesDiv);

    // Back to top link
    var backToTop = document.createElement("div");
    backToTop.style.cssText = "margin-top:2rem; padding:1rem 0; border-top:1px solid var(--border-color); text-align:center;";
    var topLink = document.createElement("a");
    topLink.href = "#research-results";
    topLink.textContent = "Back to top";
    topLink.style.cssText = "color:var(--accent-primary); text-decoration:none; font-size:0.9rem;";
    backToTop.appendChild(topLink);
    wrapper.appendChild(backToTop);

    // Clear container and add structured view
    container.textContent = "";
    container.appendChild(wrapper);
  }

  /**
   * Render summaries into a container.
   * Uses the existing markdown renderer if available (which sanitizes via
   * DOMPurify in the main app). Falls back to textContent for plain text.
   */
  function renderSummaries(summaries, container) {
    container.replaceChildren();

    var heading = document.createElement("h3");
    heading.textContent = "Dimension Summaries (" + summaries.length + ")";
    heading.style.cssText = "margin-bottom:1rem; color:var(--text-primary);";
    container.appendChild(heading);

    for (var i = 0; i < summaries.length; i++) {
      var s = summaries[i];
      var details = document.createElement("details");
      details.style.cssText = "margin-bottom:0.75rem; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;";
      if (i === 0) details.open = true;

      var summaryEl = document.createElement("summary");
      summaryEl.style.cssText = "padding:0.75rem 1rem; cursor:pointer; font-weight:600; background:var(--bg-tertiary); color:var(--text-primary);";
      summaryEl.textContent = s.dimension_value + " (" + (s.items_referenced || 0) + " items)";
      details.appendChild(summaryEl);

      var contentDiv = document.createElement("div");
      contentDiv.style.cssText = "padding:1rem;";

      // Render as plain text
      var pre = document.createElement("pre");
      pre.style.cssText = "white-space:pre-wrap; font-family:inherit;";
      pre.textContent = s.content || "";
      contentDiv.appendChild(pre);

      details.appendChild(contentDiv);
      container.appendChild(details);
    }
  }

  /**
   * Generate summaries on demand via API
   */
  function generateSummariesOnDemand(container, btn) {
    var researchId = window.location.pathname.split("/").pop();
    var csrfToken = window.api ? window.api.getCsrfToken() : "";
    var statusEl = document.getElementById("gen-summaries-status");

    btn.disabled = true;
    if (statusEl) statusEl.textContent = "Generating summaries... this may take a minute.";

    fetch("/api/research/" + researchId + "/generate-summaries", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (data.error) throw new Error(data.error);
        var summaries = data.summaries || [];
        renderSummaries(summaries, container);
        if (statusEl) statusEl.textContent = summaries.length + " summaries generated.";
        btn.disabled = false;
        var icon = btn.querySelector("i");
        if (icon) icon.className = "fas fa-redo";
        btn.childNodes[1].textContent = " Regenerate Summaries";
      })
      .catch(function (err) {
        if (statusEl) statusEl.textContent = "Error: " + err.message;
        btn.disabled = false;
      });
  }

  /**
   * Show CSV/JSON export buttons for structured results
   */
  function showStructuredExportButtons(responseData) {
    var exportItems = document.querySelectorAll(".structured-export-only");
    for (var i = 0; i < exportItems.length; i++) {
      exportItems[i].style.display = "";
    }

    // Wire up CSV export
    var csvBtn = document.getElementById("export-csv-btn");
    if (csvBtn) {
      csvBtn.addEventListener("click", function (e) {
        e.preventDefault();
        exportStructured("csv");
      });
    }

    // Wire up JSON export
    var jsonBtn = document.getElementById("export-json-btn");
    if (jsonBtn) {
      jsonBtn.addEventListener("click", function (e) {
        e.preventDefault();
        exportStructured("json");
      });
    }
  }

  /**
   * Trigger a structured export download
   */
  function exportStructured(format) {
    var researchId = window.location.pathname.split("/").pop();
    var csrfToken = window.api ? window.api.getCsrfToken() : "";

    fetch("/api/v1/research/" + researchId + "/export/" + format, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error("Export failed: " + resp.status);
        var filename = "research_export." + (format === "csv" ? "zip" : format);
        var disposition = resp.headers.get("Content-Disposition");
        if (disposition) {
          var match = disposition.match(/filename="?([^"]+)"?/);
          if (match) filename = match[1];
        }
        return resp.blob().then(function (blob) {
          return { blob: blob, filename: filename };
        });
      })
      .then(function (result) {
        var url = URL.createObjectURL(result.blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = result.filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      })
      .catch(function (err) {
        console.error("Export error:", err);
        alert("Export failed: " + err.message);
      });
  }

  // Public API
  window.structuredResults = {
    tryRenderStructured: tryRenderStructured,
    renderStructuredData: renderStructuredData,
    exportStructured: exportStructured,
  };
})();
