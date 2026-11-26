export default function AnswerEvidence() {
  const open = (ref) => callAction({name: "open_source", payload: ref.source});
  return <section aria-label="Утверждения и основания">
    {props.groups.map((group, index) => <div key={index}>
      {group.label && <h3 style={{fontWeight: 650}}>{group.label}</h3>}
      {!group.items.length && <p>{group.empty}</p>}
      {group.items.map((item, i) => <p key={i} style={{marginBlock: ".5em", overflowWrap: "anywhere"}}>
        {item.refs.length
          ? <button type="button" title="Показать основание" style={{textAlign: "start"}}
              onClick={() => open(item.refs[0])}>{item.text}</button>
          : item.text}
        {item.refs.map(ref => <button key={ref.label} type="button" onClick={() => open(ref)}
          style={{marginInlineStart: ".4em", textDecoration: "underline"}}>{ref.label}</button>)}
      </p>)}
    </div>)}
  </section>;
}
