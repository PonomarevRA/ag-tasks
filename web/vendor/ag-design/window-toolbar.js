import { createButton } from "./primitives.js";

/** Keep icon-only commands labelled; commands without an icon retain their visible text. */
function createToolbarButton({ document, label, icon, iconOnly, disabled, onClick }) {
  return createButton({
    document, label, icon, variant: "quiet",
    iconOnly: Boolean(icon) && (iconOnly ?? true), disabled, onClick,
  });
}

/** Build the web fallback for a contextual native toolbar. The host owns real OS controls, history and command capabilities. */
export function createWindowToolbar({
  document = globalThis.document,
  title = "Обзор",
  identity = "",
  onBack,
  onForward,
  onToggleLeft,
  onToggleRight,
  actions = [],
} = {}) {
  const root = document.createElement("header");
  root.className = "ag-window-toolbar";
  root.dataset.agComponent = "window-toolbar";
  root.setAttribute(
    "aria-label",
    identity ? `${identity}: панель окна` : "Панель окна",
  );
  const navigation = document.createElement("div");
  navigation.className = "ag-window-toolbar__navigation";
  const back = createToolbarButton({
    document,
    label: "Назад",
    icon: "back",
    iconOnly: true,
    disabled: true,
    onClick: onBack,
  });
  const forward = createToolbarButton({
    document,
    label: "Вперёд",
    icon: "forward",
    iconOnly: true,
    disabled: true,
    onClick: onForward,
  });
  back.dataset.agWindowCommand = "back";
  forward.dataset.agWindowCommand = "forward";
  navigation.append(back, forward);
  if (onToggleLeft)
    navigation.append(
      createToolbarButton({
        document,
        label: "Левая панель",
        icon: "panel-left",
        iconOnly: true,
        onClick: onToggleLeft,
      }),
    );
  const heading = document.createElement("strong");
  heading.className = "ag-window-toolbar__title";
  const commands = document.createElement("div");
  commands.className = "ag-window-toolbar__actions";
  const right = onToggleRight
    ? createToolbarButton({
        document,
        label: "Правая панель",
        icon: "panel-right",
        iconOnly: true,
        onClick: onToggleRight,
      })
    : null;
  root.append(navigation, heading, commands);
  if (right) root.append(right);
  /** Render contextual command state; labels cannot execute code or replace the application's navigation history. */
  function setContext({
    title: nextTitle = title,
    canBack = false,
    canForward = false,
    actions: nextActions = actions,
  } = {}) {
    heading.textContent = String(nextTitle).slice(0, 256);
    heading.title = String(nextTitle).slice(0, 256);
    back.disabled = !onBack || !canBack;
    forward.disabled = !onForward || !canForward;
    commands.replaceChildren();
    for (const action of nextActions.slice(0, 6))
      commands.append(
        createToolbarButton({
          document,
          label: action.label,
          icon: action.icon,
          iconOnly: action.iconOnly,
          disabled: action.disabled ?? false,
          onClick: action.onClick,
        }),
      );
  }
  setContext();
  return {
    element: root,
    setContext,
    dispose() {
      commands.replaceChildren();
      root.remove();
    },
  };
}
