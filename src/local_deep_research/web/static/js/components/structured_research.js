/**
 * Structured Research UI Component
 *
 * Manages the dimensions builder (with nested child levels), fields builder,
 * template loading, and submission for structured research mode.
 *
 * All dynamic HTML is built with DOM APIs (createElement/textContent)
 * to avoid innerHTML-based XSS risks.
 */
(function () {
  "use strict";

  const panel = () => document.getElementById("structured-config-panel");
  const dimsContainer = () => document.getElementById("structured-dimensions");
  const fieldsContainer = () => document.getElementById("structured-fields");
  const templateSelect = () => document.getElementById("structured-template");

  let dimCounter = 0;
  let fieldCounter = 0;

  // -------------------------------------------------------------------
  // Panel visibility — show/hide when mode changes
  // -------------------------------------------------------------------

  function onModeChange() {
    const selected = document.querySelector(
      'input[name="research_mode"]:checked'
    );
    const p = panel();
    if (!p) return;
    p.style.display = selected && selected.value === "structured" ? "" : "none";
  }

  // -------------------------------------------------------------------
  // DOM builder helpers
  // -------------------------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "style" && typeof v === "object") {
          Object.assign(node.style, v);
        } else if (k === "className") {
          node.className = v;
        } else if (k === "textContent") {
          node.textContent = v;
        } else if (k.startsWith("on") && typeof v === "function") {
          node.addEventListener(k.slice(2).toLowerCase(), v);
        } else {
          node.setAttribute(k, v);
        }
      }
    }
    if (children) {
      for (const child of Array.isArray(children) ? children : [children]) {
        if (typeof child === "string") {
          node.appendChild(document.createTextNode(child));
        } else if (child) {
          node.appendChild(child);
        }
      }
    }
    return node;
  }

  // -------------------------------------------------------------------
  // Dimensions builder (supports nested children)
  // -------------------------------------------------------------------

  /**
   * Add a dimension row to the given container.
   * @param {Object} opts - Dimension options (name, values, discover, etc.)
   * @param {HTMLElement} [targetContainer] - Where to append. Defaults to top-level dims container.
   * @returns {HTMLElement} The created row element.
   */
  function addDimension(opts, targetContainer) {
    opts = opts || {};
    const idx = dimCounter++;

    const nameInput = el("input", {
      type: "text", className: "ldr-input dim-name",
      placeholder: "Dimension name (e.g. country)",
      value: opts.name || "",
      style: { width: "160px" },
    });

    const valuesInput = el("input", {
      type: "text", className: "ldr-input dim-values",
      placeholder: "Values (comma-separated)",
      value: (opts.values || []).join(", "),
      style: { flex: "1", minWidth: "160px" },
    });

    const discoverCb = el("input", {
      type: "checkbox", className: "dim-discover",
    });
    if (opts.discover) discoverCb.checked = true;

    const discoverLabel = el("label", {
      style: { whiteSpace: "nowrap", cursor: "pointer", fontSize: "0.85rem", color: "var(--text-muted)" },
    }, [discoverCb, " Discover"]);

    // "Add Child Level" button
    const addChildBtn = el("button", {
      type: "button", className: "ldr-btn-small dim-add-child",
      title: "Add a child dimension level (e.g. sector under country)",
    }, [
      el("i", { className: "fas fa-level-down-alt", "aria-hidden": "true" }),
      " Child",
    ]);

    const removeBtn = el("button", {
      type: "button", className: "ldr-btn-icon dim-remove", title: "Remove dimension",
      textContent: "\u00d7",
    });

    const topRow = el("div", {
      style: { display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap",
               marginBottom: "0.5rem", padding: "0.75rem",
               background: "var(--bg-tertiary)", borderRadius: "6px" },
    }, [nameInput, valuesInput, discoverLabel, addChildBtn, removeBtn]);

    // Discover options
    const promptInput = el("input", {
      type: "text", className: "ldr-input dim-prompt",
      placeholder: "Discovery prompt (use {parent_value} as placeholder)",
      value: opts.prompt || "",
      style: { width: "100%", marginBottom: "0.25rem" },
    });

    const maxValInput = el("input", {
      type: "number", className: "ldr-input dim-max-values",
      value: String(opts.max_values || 10), min: "1", max: "50",
      style: { width: "60px" },
    });

    const maxValLabel = el("label", {
      style: { fontSize: "0.8rem", color: "var(--text-muted)" },
    }, ["Max values: ", maxValInput]);

    const discoverOpts = el("div", {
      className: "dim-discover-opts",
      style: { display: opts.discover ? "block" : "none", marginLeft: "1rem", marginBottom: "0.5rem" },
    }, [promptInput, maxValLabel]);

    const childrenDiv = el("div", { className: "dim-children" });

    const row = el("div", { className: "ldr-structured-row", "data-dim-idx": String(idx) },
      [topRow, discoverOpts, childrenDiv]);

    // Toggle discover options
    discoverCb.addEventListener("change", () => {
      discoverOpts.style.display = discoverCb.checked ? "block" : "none";
    });

    // Add child dimension
    addChildBtn.addEventListener("click", () => {
      addDimension({}, childrenDiv);
    });

    // Remove this dimension (and all its children)
    removeBtn.addEventListener("click", () => row.remove());

    // Append to target container (top-level or a parent's childrenDiv)
    const container = targetContainer || dimsContainer();
    if (container) container.appendChild(row);
    return row;
  }

  // -------------------------------------------------------------------
  // Fields builder
  // -------------------------------------------------------------------

  function addField(opts, targetContainer) {
    opts = opts || {};
    const idx = fieldCounter++;

    const nameInput = el("input", {
      type: "text", className: "ldr-input field-name",
      placeholder: "Field name", value: opts.name || "",
      style: { width: "150px" },
    });

    const typeSelect = el("select", { className: "ldr-select field-type", style: { width: "100px" } }, [
      el("option", { value: "string", textContent: "string" }),
      el("option", { value: "enum", textContent: "enum" }),
      el("option", { value: "number", textContent: "number" }),
      el("option", { value: "boolean", textContent: "boolean" }),
    ]);
    typeSelect.value = opts.type || "string";

    const descInput = el("input", {
      type: "text", className: "ldr-input field-desc",
      placeholder: "Description (optional)", value: opts.description || "",
      style: { flex: "1", minWidth: "150px" },
    });

    const optionsInput = el("input", {
      type: "text", className: "ldr-input field-options",
      placeholder: "Enum options (comma-separated)",
      value: (opts.options || []).join(", "),
      style: { width: "200px", display: opts.type === "enum" ? "block" : "none" },
    });

    const removeBtn = el("button", {
      type: "button", className: "ldr-btn-icon field-remove", title: "Remove field",
      textContent: "\u00d7",
    });

    const row = el("div", { className: "ldr-structured-row", "data-field-idx": String(idx) }, [
      el("div", {
        style: { display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap",
                 marginBottom: "0.5rem", padding: "0.5rem",
                 background: "var(--bg-tertiary)", borderRadius: "6px" },
      }, [nameInput, typeSelect, descInput, optionsInput, removeBtn]),
    ]);

    typeSelect.addEventListener("change", () => {
      optionsInput.style.display = typeSelect.value === "enum" ? "block" : "none";
    });

    removeBtn.addEventListener("click", () => row.remove());

    const container = targetContainer || fieldsContainer();
    if (container) container.appendChild(row);
    return row;
  }

  // -------------------------------------------------------------------
  // Templates (with recursive dimension tree support)
  // -------------------------------------------------------------------

  async function loadTemplates() {
    try {
      const resp = await fetch("/api/templates/structured");
      if (!resp.ok) return;
      const data = await resp.json();
      const sel = templateSelect();
      if (!sel || !data.templates) return;

      for (const t of data.templates) {
        const opt = document.createElement("option");
        opt.value = t.id;
        opt.textContent = t.name;
        opt.dataset.template = JSON.stringify(t);
        sel.appendChild(opt);
      }
    } catch (e) {
      console.warn("Failed to load structured templates:", e);
    }
  }

  function applyTemplate(templateData) {
    const dc = dimsContainer();
    const fc = fieldsContainer();
    if (dc) dc.replaceChildren();
    if (fc) fc.replaceChildren();
    dimCounter = 0;
    fieldCounter = 0;

    for (const dim of templateData.dimensions || []) {
      addDimensionTree(dim, dc);
    }
    for (const field of templateData.fields || []) {
      addField(field);
    }
  }

  /**
   * Recursively add a dimension and its children.
   */
  function addDimensionTree(dim, targetContainer) {
    const row = addDimension(dim, targetContainer);
    if (dim.children) {
      const childDiv = row.querySelector(":scope > .dim-children");
      const children = Array.isArray(dim.children) ? dim.children : [dim.children];
      for (const child of children) {
        addDimensionTree(child, childDiv);
      }
    }
  }

  // -------------------------------------------------------------------
  // Collect schema from UI (recursive)
  // -------------------------------------------------------------------

  function collectSchema(dimContainerOverride, fieldContainerOverride) {
    const dc = dimContainerOverride || dimsContainer();
    const dimensions = [];
    if (dc) {
      // Only collect top-level rows (direct children of the container)
      for (const row of dc.children) {
        const dim = collectDimension(row);
        if (dim) dimensions.push(dim);
      }
    }

    const fields = [];
    const fc = fieldContainerOverride || fieldsContainer();
    const fieldRows = fc?.children || [];
    for (const row of fieldRows) {
      const name = row.querySelector(".field-name")?.value?.trim();
      if (!name) continue;

      const type = row.querySelector(".field-type")?.value || "string";
      const description = row.querySelector(".field-desc")?.value?.trim() || "";
      const optionsStr = row.querySelector(".field-options")?.value?.trim();
      const options =
        type === "enum" && optionsStr
          ? optionsStr.split(",").map((v) => v.trim()).filter(Boolean)
          : undefined;

      const field = { name, type };
      if (description) field.description = description;
      if (options) field.options = options;
      fields.push(field);
    }

    const iterEl = document.getElementById("structured-iterations");
    const maxItemsEl = document.getElementById("structured-max-items");
    const maxCellsEl = document.getElementById("structured-max-cells");
    const crossContextEl = document.getElementById("structured-cross-context");
    const enrichmentEl = document.getElementById("structured-enrichment-mode");

    const opts = {
      iterations_per_cell: parseInt(iterEl?.value) || 1,
      max_items_per_cell: parseInt(maxItemsEl?.value) || 10,
      max_cells: parseInt(maxCellsEl?.value) || 200,
    };

    if (crossContextEl) opts.cross_cell_context = crossContextEl.checked;
    if (enrichmentEl) opts.enrichment_mode = enrichmentEl.value;

    const genSummariesEl = document.getElementById("structured-generate-summaries");
    if (genSummariesEl) opts.generate_summaries = genSummariesEl.checked;

    const priorEl = document.getElementById("structured-prior-research");
    if (priorEl && priorEl.value) opts.prior_research_id = priorEl.value;

    return { dimensions, fields, options: opts };
  }

  /**
   * Recursively collect a dimension and its children from a row element.
   */
  function collectDimension(row) {
    if (!row.classList.contains("ldr-structured-row")) return null;

    const name = row.querySelector(":scope > div > .dim-name")?.value?.trim()
              || row.querySelector(".dim-name")?.value?.trim();
    if (!name) return null;

    const valuesStr = row.querySelector(":scope > div > .dim-values")?.value?.trim()
                   || row.querySelector(".dim-values")?.value?.trim();
    const values = valuesStr
      ? valuesStr.split(",").map((v) => v.trim()).filter(Boolean)
      : [];

    const discover = row.querySelector(":scope > div > label > .dim-discover")?.checked
                  || row.querySelector(".dim-discover")?.checked || false;
    const prompt = row.querySelector(".dim-prompt")?.value?.trim() || "";
    const maxValues = parseInt(row.querySelector(".dim-max-values")?.value) || 10;

    const dim = { name, values };
    if (discover) {
      dim.discover = true;
      dim.prompt = prompt;
      dim.max_values = maxValues;
    }

    // Recursively collect children
    const childrenDiv = row.querySelector(":scope > .dim-children");
    if (childrenDiv && childrenDiv.children.length) {
      if (childrenDiv.children.length === 1) {
        const childDim = collectDimension(childrenDiv.children[0]);
        if (childDim) dim.children = childDim;
      } else {
        const childDims = [];
        for (const childRow of childrenDiv.children) {
          const childDim = collectDimension(childRow);
          if (childDim) childDims.push(childDim);
        }
        if (childDims.length) dim.children = childDims;
      }
    }

    return dim;
  }

  // -------------------------------------------------------------------
  // Submit structured research
  // -------------------------------------------------------------------

  async function submitStructuredResearch(query) {
    const schema = collectSchema();

    if (!schema.dimensions.length) {
      alert("Please add at least one dimension.");
      return null;
    }
    if (!schema.fields.length) {
      alert("Please add at least one field to extract.");
      return null;
    }

    const body = {
      query: query,
      dimensions: schema.dimensions,
      fields: schema.fields,
      options: schema.options,
    };

    try {
      const csrfToken = window.api ? window.api.getCsrfToken() : "";
      const resp = await fetch("/api/research/structured", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify(body),
      });
      return await resp.json();
    } catch (e) {
      console.error("Structured research submit failed:", e);
      return { status: "error", message: String(e) };
    }
  }

  // -------------------------------------------------------------------
  // Initialization
  // -------------------------------------------------------------------

  function init() {
    document.querySelectorAll('input[name="research_mode"]').forEach((radio) => {
      radio.addEventListener("change", onModeChange);
    });

    document.querySelectorAll(".ldr-mode-option").forEach((label) => {
      label.addEventListener("click", () => setTimeout(onModeChange, 10));
    });

    const addDimBtn = document.getElementById("add-dimension-btn");
    if (addDimBtn) addDimBtn.addEventListener("click", () => addDimension());

    const addFieldBtn = document.getElementById("add-field-btn");
    if (addFieldBtn) addFieldBtn.addEventListener("click", () => addField());

    const tsel = templateSelect();
    if (tsel) {
      tsel.addEventListener("change", () => {
        const opt = tsel.selectedOptions[0];
        if (opt && opt.dataset.template) {
          try {
            applyTemplate(JSON.parse(opt.dataset.template));
          } catch (e) {
            console.warn("Failed to apply template:", e);
          }
        }
      });
    }

    loadTemplates();
    loadPriorResearch();
    loadSettingsDefaults();
    onModeChange();
  }

  /**
   * Load structured research defaults from the settings API.
   * Falls back to data-default attributes on the HTML elements.
   */
  async function loadSettingsDefaults() {
    // Find all elements with data-setting attribute
    const els = document.querySelectorAll("[data-setting]");
    if (!els.length) return;

    try {
      const resp = await fetch("/settings/api");
      if (!resp.ok) throw new Error("Settings API failed");
      const allSettings = await resp.json();

      for (const el of els) {
        const key = el.dataset.setting;
        const fallback = el.dataset.default || "";
        const setting = allSettings[key];
        const val = setting && setting.value != null ? setting.value : fallback;

        if (el.type === "checkbox") {
          el.checked = val === true || val === "true" || val === 1;
        } else if (el.tagName === "SELECT") {
          el.value = String(val);
        } else {
          el.value = String(val);
        }
      }
    } catch (e) {
      // Fallback: use data-default attributes
      for (const el of els) {
        const fallback = el.dataset.default;
        if (fallback != null && !el.value) {
          if (el.type === "checkbox") {
            el.checked = fallback === "true";
          } else {
            el.value = fallback;
          }
        }
      }
    }
  }

  /**
   * Load completed structured research sessions for the "Build on prior research" dropdown.
   */
  async function loadPriorResearch() {
    var sel = document.getElementById("structured-prior-research");
    if (!sel) return;

    try {
      var resp = await fetch("/history/api");
      if (!resp.ok) return;
      var data = await resp.json();
      var items = data.items || [];

      for (var i = 0; i < items.length; i++) {
        var item = items[i];
        if (item.mode === "structured" && item.status === "completed") {
          var opt = document.createElement("option");
          opt.value = item.id;
          var date = item.created_at ? item.created_at.substring(0, 10) : "";
          opt.textContent = (item.query || "Untitled").substring(0, 60) + " (" + date + ")";
          sel.appendChild(opt);
        }
      }
    } catch (e) {
      console.warn("Failed to load prior research list:", e);
    }
  }

  // -------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------

  window.structuredResearch = {
    init,
    collectSchema,
    submitStructuredResearch,
    addDimension,
    addField,
    applyTemplate,
    addDimensionTree,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
