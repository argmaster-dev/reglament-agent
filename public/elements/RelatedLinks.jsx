export default function RelatedLinks() {
  const open = (source) => callAction({name: "open_source", payload: source});
  const related = props.related || [];
  return <aside aria-label={props.title} style={{overflowWrap: "anywhere"}}>
    <div style={{display: "flex", flexWrap: "nowrap", alignItems: "center", gap: ".5em",
        overflowX: "auto", marginBlock: ".4em", fontSize: ".85em", whiteSpace: "nowrap"}}>
      <span>{props.title}</span>
      {related.map((n, i) => <button key={i} type="button" title={n.label} onClick={() => open(n.source)}
        style={{flex: "0 1 auto", minWidth: 0, maxWidth: "12em", overflow: "hidden", textOverflow: "ellipsis",
          whiteSpace: "nowrap", textDecoration: "underline", cursor: "pointer"}}>{n.label}</button>)}</div>
  </aside>;
}
