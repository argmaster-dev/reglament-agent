export default function RefusalNote() {
  const open = (source) => callAction({name: "open_source", payload: source});
  const near = props.near || [];
  const ticket = props.ticket;
  return <aside aria-label="Обращение и близкие пункты" style={{overflowWrap: "anywhere"}}>
    {near.length > 0 && <p style={{marginBlock: ".4em"}}>Близкие пункты:{" "}
      {near.map((n, i) => <button key={i} type="button" title="Показать пункт" onClick={() => open(n.source)}
        style={{marginInlineEnd: ".6em", textDecoration: "underline", cursor: "pointer"}}>{n.label}</button>)}</p>}
    {ticket && <p style={{marginBlock: ".4em", fontSize: ".78em", opacity: 0.75}}>
      <small>{ticket.text} · <a href={ticket.href} target="_blank" rel="noreferrer">{ticket.link}</a></small></p>}
  </aside>;
}
