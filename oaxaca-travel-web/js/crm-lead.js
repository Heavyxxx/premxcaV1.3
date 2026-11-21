/* Oaxaca Infinita: normalización y transporte del lead hacia el CRM. */
const CRM_CONFIG = {
  // Configura enabled y endpointUrl cuando el webhook o CRM esté listo.
  enabled: false,
  endpointUrl: 'https://tu-crm-o-webhook.com/api/leads',
  storageKey: 'oaxaca_infinita_leads'
};

function saveLeadLocally(leadData) {
  try {
    const storedLeads = JSON.parse(localStorage.getItem(CRM_CONFIG.storageKey) || '[]');
    if (!Array.isArray(storedLeads)) {
      throw new Error('El respaldo local de leads no contiene una lista válida.');
    }
    storedLeads.push(leadData);
    localStorage.setItem(CRM_CONFIG.storageKey, JSON.stringify(storedLeads));
    return { ok: true };
  } catch (error) {
    console.error('[CRM Local] No se pudo guardar el lead en este navegador:', error);
    return { ok: false, error };
  }
}

function createStructuredLead(rawPayload) {
  const ticketsCount = Number(rawPayload.tickets_count);
  if (![1, 2].includes(ticketsCount)) {
    throw new Error('La cotización debe incluir uno o dos boletos.');
  }

  const ticketPrice = 120000;
  const discount = 0.15;
  const savings = ticketsCount === 2 ? ticketPrice * discount : 0;
  const estimatedTotal = ticketPrice + (ticketsCount === 2 ? ticketPrice - savings : 0);

  return {
    leadId: `LEAD-${Date.now()}`,
    createdAt: new Date().toISOString(),
    source: rawPayload.source || 'web_home',
    packageInterested: rawPayload.package || 'Expedición privada Oaxaca Infinita',
    selectedDate: rawPayload.trip_edition_or_month || rawPayload.date || 'Por confirmar',
    ticket_price: ticketPrice,
    tickets_count: ticketsCount,
    discount_applied_second_ticket: discount,
    estimated_total_mxn: estimatedTotal,
    savings_mxn: savings,
    trip_edition_or_month: rawPayload.trip_edition_or_month || 'Por confirmar',
    route_sequence: ['Mixteca', 'Huazolotitlán', 'Puerto Ángel'],
    instant_quote_promo: true,
    guests: ticketsCount,
    contact: {
      name: (rawPayload.name || '').trim(),
      phone: (rawPayload.phone || '').trim(),
      email: (rawPayload.email || '').trim()
    },
    status: 'nuevo_prospecto'
  };
}

async function sendLeadToCRM(rawPayload) {
  let structuredLead;
  try {
    structuredLead = createStructuredLead(rawPayload);
  } catch (error) {
    console.error('[CRM Error] Datos de cotización inválidos:', error);
    return { ok: false, error };
  }

  const localResult = saveLeadLocally(structuredLead);

  if (CRM_CONFIG.enabled && CRM_CONFIG.endpointUrl) {
    try {
      const response = await fetch(CRM_CONFIG.endpointUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(structuredLead)
      });
      if (!response.ok) throw new Error(`El CRM respondió HTTP ${response.status}.`);
      return { ok: true, data: structuredLead, localBackup: localResult.ok };
    } catch (error) {
      console.error('[CRM Error] Falló el envío remoto:', error);
      return { ok: false, data: structuredLead, localBackup: localResult.ok, error };
    }
  }

  if (!localResult.ok) return { ok: false, data: structuredLead, error: localResult.error };
  await new Promise((resolve) => window.setTimeout(resolve, 300));
  return { ok: true, data: structuredLead, simulated: true };
}
