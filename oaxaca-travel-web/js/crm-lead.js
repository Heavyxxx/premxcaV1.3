(function () {
  const TICKET_PRICE = 120000;
  const SECOND_TICKET_DISCOUNT = 0.15;
  const editions = ["Febrero", "Abril", "Junio", "Septiembre", "Noviembre"];
  const money = new Intl.NumberFormat("es-MX", {
    style: "currency",
    currency: "MXN",
    maximumFractionDigits: 0
  });
  let confirmationFrame = 0;
  let confirmationTimer = 0;

  function friendlyError(message) {
    const error = new Error(message);
    error.isFriendly = true;
    return error;
  }

  async function submitQuoteToCRM(payload) {
    let response;
    try {
      response = await fetch("/api/leads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
    } catch (error) {
      throw friendlyError("No pudimos conectar con el concierge. Revisa tu conexión e inténtalo de nuevo.");
    }

    let result;
    if (!response.ok) {
      try {
        result = JSON.parse(await response.text());
      } catch (error) {
        result = null;
      }
      const message = result && typeof result.error === "string"
        ? result.error
        : "No se pudo registrar la cotización. Inténtalo de nuevo.";
      throw friendlyError(message);
    }

    try {
      result = JSON.parse(await response.text());
    } catch (error) {
      result = null;
    }
    const responseLead = result && typeof result === "object" && !Array.isArray(result)
      ? result.lead
      : null;
    return {
      ...(result && typeof result === "object" && !Array.isArray(result) ? result : {}),
      lead: responseLead && typeof responseLead === "object" && !Array.isArray(responseLead)
        ? responseLead
        : payload
    };
  }

  function numberOrFallback(value, fallback) {
    if (value === null || value === undefined || value === "") {
      return Number(fallback);
    }
    const number = Number(value);
    return Number.isFinite(number) ? number : Number(fallback);
  }

  function displayConfirmation(lead, payload, form) {
    const name = lead.name || lead.full_name || payload.name;
    const tickets = numberOrFallback(lead.tickets ?? lead.tickets_count ?? lead.guests, payload.tickets);
    const departure = lead.departure || lead.trip_edition_or_month || lead.date || payload.departure;
    const discountedTickets = numberOrFallback(
      lead.discounted_tickets,
      payload.discounted_tickets ?? 0
    );
    const discount = numberOrFallback(
      lead.discount_amount ?? lead.savings_mxn,
      payload.discount_amount ?? payload.savings_mxn ?? 0
    );
    const total = numberOrFallback(
      lead.total ?? lead.estimated_total_mxn ?? lead.total_mxn,
      payload.estimated_total_mxn
    );
    const contacts = [payload.phone, payload.email].filter(Boolean).join(" · ");

    document.getElementById("confirmationName").textContent = name;
    document.getElementById("confirmationTickets").textContent =
      `${tickets} ${tickets === 1 ? "boleto" : "boletos"}`;
    document.getElementById("confirmationDeparture").textContent = departure;
    document.getElementById("confirmationTotal").textContent = money.format(total);
    document.getElementById("confirmationMessage").textContent =
      `¡Gracias, ${name}! Recibirás información en breve por parte del equipo concierge de PREMXCA${contacts ? ` en ${contacts}` : ""}.`;

    const discountRow = document.getElementById("confirmationDiscountRow");
    discountRow.hidden = discountedTickets < 1;
    if (discountedTickets > 0) {
      document.getElementById("confirmationDiscount").textContent =
        `${discountedTickets} ${discountedTickets === 1 ? "boleto" : "boletos"} · ahorro de ${money.format(discount)}`;
    }

    const feedback = document.getElementById("crmFeedback");
    feedback.textContent = "";
    feedback.classList.remove("is-error");
    document.getElementById("modalQuoteSummary").hidden = true;

    const confirmation = document.getElementById("quoteConfirmation");
    confirmation.hidden = false;
    confirmation.classList.add("is-entering");
    confirmationFrame = window.requestAnimationFrame(() => {
      form.classList.add("is-exiting");
      confirmation.classList.remove("is-entering");
      confirmation.classList.add("is-visible");
    });
    confirmationTimer = window.setTimeout(() => {
      form.hidden = true;
      form.classList.remove("is-exiting");
      confirmationTimer = 0;
      document.getElementById("btnConfirmationClose").focus();
    }, 320);
  }

  function resetModal() {
    window.cancelAnimationFrame(confirmationFrame);
    window.clearTimeout(confirmationTimer);
    confirmationFrame = 0;
    confirmationTimer = 0;
    const form = document.getElementById("modalCrmForm");
    const confirmation = document.getElementById("quoteConfirmation");
    form.reset();
    form.hidden = false;
    form.classList.remove("is-exiting");
    confirmation.hidden = true;
    confirmation.classList.remove("is-entering", "is-visible");
    document.getElementById("confirmationDiscountRow").hidden = true;
    document.getElementById("modalQuoteSummary").hidden = false;

    const feedback = document.getElementById("crmFeedback");
    feedback.textContent = "";
    feedback.classList.remove("is-error");
    const submitButton = form.querySelector('button[type="submit"]');
    submitButton.disabled = false;
    submitButton.textContent = "Enviar cotización al concierge";
  }

  document.getElementById("modalCrmForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submitButton = form.querySelector('button[type="submit"]');
    const feedback = document.getElementById("crmFeedback");
    const edition = document.getElementById("crmSelectedEdition").value;
    const tickets = Number(document.getElementById("crmTicketCount").value);
    if (!Number.isInteger(tickets) || tickets < 1 || tickets > 20) {
      feedback.textContent = "La cantidad debe ser un número entero entre 1 y 20. Vuelve al cotizador para ajustar los boletos.";
      feedback.classList.add("is-error");
      return;
    }
    const subtotal = tickets * TICKET_PRICE;
    const discountedTickets = Math.floor(tickets / 2);
    const discount = Math.round(discountedTickets * TICKET_PRICE * SECOND_TICKET_DISCOUNT);
    const editionIndex = editions.indexOf(edition);
    if (editionIndex < 0) {
      feedback.textContent = "La salida seleccionada no está disponible. Vuelve al cotizador y elige una de las cinco ventanas.";
      feedback.classList.add("is-error");
      return;
    }
    const departure = `Salida ${editionIndex + 1} • ${edition}`;
    document.getElementById("crmEstimatedTotal").value = String(subtotal - discount);
    document.getElementById("crmSavings").value = String(discount);
    const payload = {
      name: document.getElementById("crmName").value.trim(),
      email: document.getElementById("crmEmail").value.trim(),
      phone: document.getElementById("crmPhone").value.trim(),
      source: "Web · Cotizador",
      tickets,
      discounted_tickets: discountedTickets,
      departure,
      trip_edition_or_month: edition,
      subtotal,
      discount_amount: discount,
      estimated_total_mxn: Number(document.getElementById("crmEstimatedTotal").value),
      savings_mxn: Number(document.getElementById("crmSavings").value),
      notes: `Cotización web · ${edition} · ${money.format(subtotal - discount)}`
    };

    submitButton.disabled = true;
    submitButton.textContent = "Enviando cotización...";
    feedback.textContent = "";
    feedback.classList.remove("is-error");

    let result;
    try {
      result = await submitQuoteToCRM(payload);
    } catch (error) {
      console.error("[CRM] No se pudo enviar la cotización:", error);
      feedback.textContent = error && error.isFriendly
        ? error.message
        : "Ocurrió un problema al enviar la cotización. Inténtalo de nuevo en unos momentos.";
      feedback.classList.add("is-error");
      return;
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = "Enviar cotización al concierge";
    }
    displayConfirmation(result.lead, payload, form);
    form.reset();
  });

  document.getElementById("btnConfirmationClose")?.addEventListener("click", () => {
    window.closeLeadModal();
  });

  window.PremxcaCRM = { submitQuoteToCRM, resetModal };
})();
