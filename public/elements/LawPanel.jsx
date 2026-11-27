export default function LawPanel() {
  return <article aria-label="Статья закона" style={{overflowWrap:"anywhere",lineHeight:1.55}}>
    <h2 style={{fontWeight:650}}>{props.act}</h2>
    <p><strong>{props.article}</strong>. {props.title}</p>
    <p style={{whiteSpace:"pre-wrap"}}>{props.text}</p>
    <footer style={{marginTop:12}}>
      <p>{props.revision}</p>
      {props.official_publication ? <p>Официальная публикация: {props.official_publication}{props.official_url ? <> · <a href={props.official_url} target="_blank" rel="noopener noreferrer">официальный портал</a></> : null}</p> : null}
      <p>{props.source_url ? <a href={props.source_url} target="_blank" rel="noopener noreferrer">текст редакции</a> : "источник не указан"}</p>
      <p>{props.retrieved}</p>
      <p style={{fontSize:".85em"}}><em>{props.note}</em></p>
    </footer>
  </article>;
}
