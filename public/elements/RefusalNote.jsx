export default function RefusalNote() {
  const open = (source) => callAction({name: "open_source", payload: source});
  const near = props.near || [];
  const ticket = props.ticket;
  return <aside aria-label="Обращение и близкие пункты" style={{overflowWrap: "anywhere"}}>
    {near.length > 0 && <div style={{display: "flex", flexWrap: "nowrap", alignItems: "center", gap: ".5em",
        overflowX: "auto", marginBlock: ".4em", fontSize: ".85em", whiteSpace: "nowrap"}}>
      <span>Близкие пункты:</span>
      {near.map((n, i) => <button key={i} type="button" title={n.label} onClick={() => open(n.source)}
        style={{flex: "0 1 auto", minWidth: 0, maxWidth: "12em", overflow: "hidden", textOverflow: "ellipsis",
          whiteSpace: "nowrap", textDecoration: "underline", cursor: "pointer"}}>{n.label}</button>)}</div>}
    {ticket && <p style={{marginBlock: ".4em", fontSize: ".78em", opacity: 0.75}}>
      <small>{ticket.text} · <a href={ticket.href} target="_blank" rel="noreferrer">{ticket.link}</a></small></p>}
  </aside>;
}
