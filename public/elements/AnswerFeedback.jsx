import {useState} from "react";

export default function AnswerFeedback() {
  const [reason, setReason] = useState("");
  const [comment, setComment] = useState("");
  const [phase, setPhase] = useState("edit");
  const [error, setError] = useState("");
  async function submit(event) {
    event.preventDefault();
    if (!reason || phase === "saving") return;
    setPhase("saving");
    try {
      const response = await callAction({
        name: "save_feedback",
        payload: {answer_id: props.answer_id, reason, comment}
      });
      if (!response?.success) throw new Error();
      setPhase("done");
    } catch {
      setError("Не удалось сохранить отзыв. Повторите попытку.");
      setPhase("edit");
    }
  }
  if (phase === "done") return <p role="status">Отзыв сохранён.</p>;
  return <form onSubmit={submit} style={{display:"grid",gap:"1rem"}}>
    <fieldset><legend>Что не так с ответом?</legend>
      {props.reasons.map(item => <label key={item} style={{display:"block",padding:".3rem 0"}}>
        <input type="radio" name="reason" value={item} checked={reason===item}
          onChange={() => setReason(item)} /> {item}
      </label>)}
    </fieldset>
    <label>Комментарий (необязательно)
      <textarea maxLength={1500} rows={4} value={comment} onChange={e=>setComment(e.target.value)}
        style={{display:"block",width:"100%",padding:".5rem",border:"1px solid",borderRadius:6}} />
    </label>
    <button type="submit" disabled={!reason || phase==="saving"}
      style={{padding:".6rem",border:"1px solid",borderRadius:6}}>
      {phase==="saving" ? "Сохранение…" : "Сохранить отзыв"}
    </button>
    {error && <p role="alert">{error}</p>}
  </form>;
}
