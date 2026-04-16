/**
 * Structured Research Refinement Component
 *
 * Shown on the results page for structured research sessions.
 * Pre-populates dimensions and fields from the completed research schema,
 * allows editing, and triggers re-run via the structured API.
 *
 * Reuses addDimension(), addField(), and collectSchema() from
 * structured_research.js (which must be loaded first).
 */
(function () {
  "use strict";

  var researchId = null;
  var dimsContainer = null;
  var fieldsContainer = null;

  /**
   * Initialize the refinement panel if this is a structured research result.
   * Called by structured_results.js after rendering.
   */
  function init(responseData) {
    var mode = responseData.metadata && responseData.metadata.mode;
    if (mode !== "structured") return;

    var panel = document.getElementById("structured-refinement-panel");
    if (!panel) return;

    researchId = window.location.pathname.split("/").pop();
    dimsContainer = document.getElementById("refine-dimensions");
    fieldsContainer = document.getElementById("refine-fields");

    if (!dimsContainer || !fieldsContainer) return;

    // Show the panel
    panel.style.display = "";

    // Wire toggle
    var toggle = document.getElementById("refinement-toggle");
    var content = document.getElementById("refinement-content");
    if (toggle && content) {
      toggle.addEventListener("click", function () {
        var expanded = content.style.display !== "none";
        content.style.display = expanded ? "none" : "";
        toggle.setAttribute("aria-expanded", String(!expanded));
        var icon = toggle.querySelector(".fa-chevron-down, .fa-chevron-up");
        if (icon) {
          icon.className = icon.className.replace(
            expanded ? "fa-chevron-up" : "fa-chevron-down",
            expanded ? "fa-chevron-down" : "fa-chevron-up"
          );
        }
      });
    }

    // Pre-populate from schema
    var sd = responseData.structured_data;
    if (sd && sd.schema) {
      populateFromSchema(sd.schema);
    }

    // Wire buttons
    var addDimBtn = document.getElementById("refine-add-dimension-btn");
    if (addDimBtn) {
      addDimBtn.addEventListener("click", function () {
        if (window.structuredResearch) {
          window.structuredResearch.addDimension({}, dimsContainer);
        }
      });
    }

    var addFieldBtn = document.getElementById("refine-add-field-btn");
    if (addFieldBtn) {
      addFieldBtn.addEventListener("click", function () {
        if (window.structuredResearch) {
          window.structuredResearch.addField({}, fieldsContainer);
        }
      });
    }

    var rerunBtn = document.getElementById("refine-rerun-btn");
    if (rerunBtn) {
      rerunBtn.addEventListener("click", function () {
        rerunResearch();
      });
    }
  }

  /**
   * Populate dimensions and fields from the schema definition.
   */
  function populateFromSchema(schema) {
    if (!window.structuredResearch) return;

    // Clear containers
    dimsContainer.replaceChildren();
    fieldsContainer.replaceChildren();

    // Add dimensions (recursive tree)
    var dims = schema.dimensions || [];
    for (var i = 0; i < dims.length; i++) {
      window.structuredResearch.addDimensionTree(dims[i], dimsContainer);
    }

    // Add fields
    var fields = schema.fields || [];
    for (var j = 0; j < fields.length; j++) {
      window.structuredResearch.addField(fields[j], fieldsContainer);
    }
  }

  /**
   * Collect the refined schema and re-run the research.
   */
  async function rerunResearch() {
    if (!window.structuredResearch || !researchId) return;

    var statusEl = document.getElementById("refine-status");
    var rerunBtn = document.getElementById("refine-rerun-btn");

    // Collect schema from the refinement containers
    var schema = window.structuredResearch.collectSchema(dimsContainer, fieldsContainer);

    if (!schema.dimensions.length) {
      alert("Please add at least one dimension.");
      return;
    }
    if (!schema.fields.length) {
      alert("Please add at least one field.");
      return;
    }

    if (rerunBtn) rerunBtn.disabled = true;
    if (statusEl) statusEl.textContent = "Updating schema...";

    try {
      var csrfToken = window.api ? window.api.getCsrfToken() : "";

      // Update the schema via PATCH
      var patchResp = await fetch("/api/research/" + researchId + "/dimensions", {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({
          schema_definition: {
            query: document.getElementById("result-query")
              ? document.getElementById("result-query").textContent
              : "",
            dimensions: schema.dimensions,
            fields: schema.fields,
            options: schema.options,
          },
        }),
      });

      if (!patchResp.ok) {
        var patchData = await patchResp.json();
        throw new Error(patchData.error || "Failed to update schema");
      }

      if (statusEl) statusEl.textContent = "Starting re-run...";

      // Execute
      var execResp = await fetch("/api/research/" + researchId + "/execute", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
      });

      if (!execResp.ok) {
        var execData = await execResp.json();
        throw new Error(execData.error || "Failed to start execution");
      }

      // Redirect to progress page
      window.location.href = "/progress/" + researchId;
    } catch (e) {
      if (statusEl) statusEl.textContent = "Error: " + e.message;
      if (rerunBtn) rerunBtn.disabled = false;
    }
  }

  // Public API
  window.structuredRefinement = {
    init: init,
  };
})();
