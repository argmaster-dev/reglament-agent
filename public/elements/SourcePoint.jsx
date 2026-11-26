export default function SourcePoint() {
  const mark = (text, selected) => selected
    ? <mark style={{background:"#ffde80",color:"#282012", padding:"0 .12em"}}>{text}</mark> : text;
  return <article aria-label="Основание ответа" style={{overflowWrap:"anywhere",lineHeight:1.55}}>
    <h2 style={{fontWeight:650}}>{props.title}</h2>
    <p>{props.path.join(" › ")}</p>
    {props.entries.map((entry,i) => entry.cells.length
       ? <dl key={i} style={{borderBlockStart:"1px solid #9ca3af",paddingBlock:6}}>
           {entry.cells.map(cell=><div key={cell.header}><dt style={{fontWeight:600}}>{cell.header}</dt>
             <dd>{mark(cell.text,cell.selected)}</dd></div>)}</dl>
       : <p key={i} style={{marginBlock:8}}>{mark(entry.text,entry.selected)}</p>)}
    {props.neighbours.length>0 && <details><summary>Соседние пункты</summary>
      <ul>{props.neighbours.map(([number,text])=><li key={number}>{number}. {text}</li>)}</ul></details>}
    <footer style={{marginTop:12}}>
      {props.column && <p>Столбец таблицы: {props.column}</p>}
      <p>{[props.department,...props.stages].filter(Boolean).join(" · ")}</p>
      <p>Версия {props.version}, загружена {props.uploaded}</p>
    </footer>
  </article>;
}
