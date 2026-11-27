export default function LawPanel() {
  return <article aria-label="Статья закона" style={{overflowWrap:"anywhere",lineHeight:1.55}}>
    <h2 style={{fontWeight:650}}>{props.act}</h2>
    <p><strong>{props.article}</strong>. {props.title}</p>
    <p style={{whiteSpace:"pre-wrap"}}>{props.text}</p>
    <footer style={{marginTop:12}}>
      <p>{props.revision}</p>
      <p>{props.source_url ? <a href={props.source_url} target="_blank" rel="noopener noreferrer">источник</a> : "источник не указан"}</p>
      <p>{props.retrieved}</p>
      <p><em>{props.note}</em></p>
    </footer>
  </article>;
}
