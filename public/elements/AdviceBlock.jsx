export default function AdviceBlock() {
  const open = (law_id) => callAction({name: "open_law", payload: {law_id}});
  return <aside aria-label={props.title} style={{overflowWrap: "anywhere", marginBlock: ".6em"}}>
    <p style={{marginBlock: ".2em", fontWeight: 650}}>{props.title}</p>
    <p style={{marginBlock: ".2em", fontSize: ".78em", opacity: 0.75}}><small><em>{props.note}</em></small></p>
    <ul>{props.items.map((item, i) => <li key={i}>{item.text}{" ("}
      {item.laws.map((law, j) => <span key={law.law_id}>{j > 0 && "; "}
        <button type="button" title="Показать статью" onClick={() => open(law.law_id)}
          style={{textDecoration: "underline", cursor: "pointer"}}>{law.label}</button></span>)}{")"}</li>)}</ul>
  </aside>;
}
