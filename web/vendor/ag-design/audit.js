import { auditLayoutWidths } from "./width-audit.js";
export { auditLayoutWidths };
import { auditFormRows } from "./form-row-audit.js";
export { auditFormRows };

/** WCAG relative luminance contrast for opaque RGB colours, not APCA or native-platform certification. */
export function contrastRatio(first, second) {
  const luminance = (channels) => {
    const linear = channels.map((channel) => {
      const value = channel / 255;
      return value <= 0.04045
        ? value / 12.92
        : ((value + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  };
  const a = luminance(first),
    b = luminance(second);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** Audit bounded visible controls. Reports are signals; manual checks remain mandatory. No source execution or network. */
export function auditInterface(root, { maxElements = 500 } = {}) {
  if (!Number.isInteger(maxElements) || maxElements < 1 || maxElements > 2000)
    throw new RangeError("maxElements must be 1..2000");
  const findings = [],
    document = root.ownerDocument ?? root;
  const controls = root.querySelectorAll(
    "button,input,select,textarea,a[href]",
  );
  const add = (element, ruleId, summary) =>
    findings.push({
      ruleId,
      componentId: element.dataset.agComponent,
      severity: "warning",
      summary,
      evidence: element.id ? `#${element.id}` : element.tagName.toLowerCase(),
    });
  let inspected = 0;
  for (const element of controls) {
    if (inspected >= maxElements) break;
    if (
      element.closest("[hidden],[inert]") ||
      element.type === "hidden" ||
      element.getClientRects().length === 0
    )
      continue;
    inspected++;
    const labelledBy = (element.getAttribute("aria-labelledby") ?? "")
      .split(/\s+/)
      .filter(Boolean);
    let name = element.getAttribute("aria-label") ?? "";
    for (const id of labelledBy)
      name += document.getElementById(id)?.textContent ?? "";
    for (const label of element.labels ?? []) name += label.textContent;
    if (["BUTTON", "A"].includes(element.tagName)) name += element.textContent;
    if (!name.trim())
      add(element, "accessible-name", "У элемента нет доступного названия.");
    const size = element.getBoundingClientRect();
    if (size.width < 24 || size.height < 24)
      add(
        element,
        "target-size",
        "Область нажатия меньше 24 CSS px; проверьте исключения WCAG и расстояния.",
      );
    if (
      element.hasAttribute("aria-controls") &&
      !document.getElementById(element.getAttribute("aria-controls"))
    )
      add(
        element,
        "disclosure-state",
        "aria-controls ссылается на отсутствующий элемент.",
      );
  }
  const layoutWidths = auditLayoutWidths(root);
  for (const target of layoutWidths.targets) {
    if (target.status !== "fail" && target.status !== "unknown") continue;
    findings.push({ruleId: target.status === "fail" ? "layout-width-integrity" : "layout-width-scenarios",
      severity: "warning", evidence: `${target.id}; ${target.reason}; width=${target.width}; available=${target.availableWidth}; deficit=${target.deficitPx}`,
      summary: target.status === "fail" ? "Недостаточная ширина корня или внутренней колонки." : "Ширина элемента осталась без подтверждённой проверки."});
  }
  const formRows = auditFormRows(root);
  if (formRows.coverage.undeclaredControls > 0) {
    findings.push({
      ruleId: "form-row-semantic-group",
      severity: "warning",
      evidence: `${formRows.coverage.undeclaredControls} visible text controls outside declared field groups`,
      summary: "Часть видимых полей не охвачена проверкой компоновки.",
    });
  }
  for (const row of formRows.rows) {
    for (const check of row.checks) {
      if (check.status !== "fail") continue;
      // The report store accepts catalog rule IDs and textual evidence only.
      // Keep complete numeric facts in formRows; associate overflow/order with reflow.
      const ruleId = [
        "form-row-containment",
        "form-row-reading-order",
      ].includes(check.id)
        ? "form-row-reflow-order"
        : check.id;
      findings.push({
        ruleId,
        severity: "warning",
        evidence: `${row.id}; ${check.id}: ${check.residualPx} CSS px; tolerance ${row.tolerance} CSS px`,
        summary: `Геометрия формы: ${check.id}, отклонение ${check.residualPx.toFixed(2)} CSS px (допуск ${row.tolerance}).`,
      });
    }
  }
  return {
    schemaVersion: "ag-design/audit-v1",
    inspected,
    truncated: inspected >= maxElements,
    findings,
    formRows,
    layoutWidths,
    manualRequired: [
      "keyboard-flow",
      "contrast",
      "zoom-reflow",
      "native-shell",
      "screen-reader",
    ],
    verdict: findings.length ? "review" : "manual-review-required",
  };
}
