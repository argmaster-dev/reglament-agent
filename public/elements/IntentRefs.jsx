export default function IntentRefs() {
  const open = (source) => callAction({name: "open_source", payload: source});
  const items = props.items || [];
  return <aside aria-label="Пункты к вариантам" style={{overflowWrap: "anywhere"}}>
    {items.map((it, i) => <div key={i} style={{display: "flex", flexWrap: "nowrap", alignItems: "center",
        gap: ".5em", overflowX: "auto", marginBlock: ".2em", fontSize: ".85em", whiteSpace: "nowrap"}}>
      <span title={it.option} style={{flex: "0 1 auto", minWidth: 0, maxWidth: "16em", overflow: "hidden",
        textOverflow: "ellipsis"}}>{it.option}:</span>
      <button type="button" title={it.label} onClick={() => open(it.source)}
        style={{flex: "0 1 auto", minWidth: 0, maxWidth: "12em", overflow: "hidden", textOverflow: "ellipsis",
          whiteSpace: "nowrap", textDecoration: "underline", cursor: "pointer"}}>{it.label}</button></div>)}
  </aside>;
}
