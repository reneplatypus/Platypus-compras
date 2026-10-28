// Line splits on the invoice screen: "+ dividir" adds another account/amount row to a line,
// and each line shows whether its parts add up to the line cost.
(function () {
  const form = document.getElementById("lines");
  if (!form) return;

  const cents = (v) => Math.round(parseFloat(String(v).replace(/,/g, "")) * 100) || 0;

  function check(body) {
    const cost = cents(body.dataset.cost);
    const sum = [...body.querySelectorAll("input[name^=amount_]")].reduce((s, i) => s + cents(i.value), 0);
    body.classList.toggle("unbalanced", sum !== cost);
    let note = body.querySelector(".diff");
    if (sum !== cost) {
      if (!note) {
        note = document.createElement("span");
        note.className = "diff small";
        body.querySelector("td.desc").appendChild(note);
      }
      note.textContent = " Falta asignar " + ((cost - sum) / 100).toFixed(2);
    } else if (note) {
      note.remove();
    }
  }

  form.addEventListener("click", (e) => {
    if (!e.target.classList.contains("split-btn")) return;
    const row = e.target.closest("tr");
    const body = row.closest("tbody");
    const clone = row.cloneNode(true);
    clone.querySelectorAll("td[rowspan]").forEach((td) => td.remove());
    clone.querySelector("input[name^=amount_]").value = "0.00";
    clone.querySelector("select").selectedIndex = 0;
    const remove = clone.querySelector(".split-btn");
    remove.textContent = "quitar";
    remove.className = "link remove-btn";
    body.appendChild(clone);
    body.querySelectorAll("td[rowspan]").forEach((td) => (td.rowSpan = body.rows.length));
    check(body);
  });

  form.addEventListener("click", (e) => {
    if (!e.target.classList.contains("remove-btn")) return;
    const body = e.target.closest("tbody");
    e.target.closest("tr").remove();
    body.querySelectorAll("td[rowspan]").forEach((td) => (td.rowSpan = body.rows.length));
    check(body);
  });

  form.addEventListener("input", (e) => {
    if (e.target.name && e.target.name.startsWith("amount_")) check(e.target.closest("tbody"));
  });
})();
