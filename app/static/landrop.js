(() => {
  const json = async (url, options) => {
    const response = await fetch(url, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    return body;
  };

  const formatSize = (bytes) => {
    let value = Number(bytes); const units = ['B', 'KB', 'MB', 'GB']; let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  };

  const status = (id, message, error = false) => {
    const element = document.getElementById(id); if (element) { element.textContent = message; element.classList.toggle('error', error); }
  };

  const loadPeers = async () => {
    const list = document.getElementById('peer-list'); if (!list) return;
    try {
      const data = await json('/api/peers');
      list.innerHTML = data.peers.length ? data.peers.map((peer) => `<label class="peer-card"><span><strong>${escapeHtml(peer.name)}</strong><small>${escapeHtml(peer.address)}:${peer.http_port}</small></span><input type="radio" name="peer_id" value="${escapeHtml(peer.device_id)}"></label>`).join('') : '<p class="muted">No devices found yet. Keep LanDrop open on another device on this WiFi.</p>';
    } catch (error) { list.innerHTML = `<p class="status error">${escapeHtml(error.message)}</p>`; }
  };

  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (character) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));

  const sendToPeer = document.querySelector('[data-peer-send-form]');
  if (sendToPeer) {
    loadPeers(); setInterval(loadPeers, 3000);
    sendToPeer.addEventListener('submit', async (event) => {
      event.preventDefault(); const file = document.getElementById('desktop-file').files[0]; const peer = document.querySelector('input[name="peer_id"]:checked');
      if (!file || !peer) return status('desktop-status', 'Choose a file and a device first.', true);
      event.target.querySelector('button').disabled = true; status('desktop-status', 'Offering the file to the receiver...');
      try {
        const data = await json(`/api/ui/send?peer_id=${encodeURIComponent(peer.value)}&filename=${encodeURIComponent(file.name)}`, { method: 'POST', headers: {'content-type': file.type || 'application/octet-stream'}, body: file });
        status('desktop-status', `Transfer started. Job ${data.job_id.slice(0, 8)}; waiting for receiver approval.`);
      } catch (error) { status('desktop-status', error.message, true); event.target.querySelector('button').disabled = false; }
    });
  }

  const phoneShareForm = document.querySelector('[data-phone-share-form]');
  if (phoneShareForm) phoneShareForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = document.getElementById('phone-share-file').files[0];
    const button = event.target.querySelector('button');
    if (!file) return status('phone-share-status', 'Choose a file first.', true);
    button.disabled = true;
    status('phone-share-status', 'Creating a temporary phone page...');
    try {
      const data = await json(`/api/ui/phone-share?filename=${encodeURIComponent(file.name)}`, {
        method: 'POST',
        headers: {'content-type': file.type || 'application/octet-stream'},
        body: file,
      });
      document.getElementById('phone-share-qr').src = data.qr_code;
      const link = document.getElementById('phone-share-link');
      link.href = data.url;
      link.textContent = data.url;
      document.getElementById('phone-share-result').hidden = false;
      status('phone-share-status', 'Scan the QR code on the phone, then download the file.');
    } catch (error) {
      status('phone-share-status', error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  const phoneForm = document.querySelector('[data-upload-form]');
  if (phoneForm) phoneForm.addEventListener('submit', async (event) => {
    event.preventDefault(); const file = document.getElementById('phone-file').files[0]; if (!file) return;
    const sender = document.getElementById('phone-sender').value || 'Phone browser'; event.target.querySelector('button').disabled = true; status('phone-status', 'Waiting for approval on the desktop...');
    try {
      const offer = await json('/api/transfers', { method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({filename: file.name, size: file.size, sender_name: sender}) });
      let transfer = offer;
      for (let attempt = 0; attempt < 120 && transfer.status === 'pending'; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 1000)); transfer = await json(`/api/transfers/${offer.transfer_id}`); }
      if (transfer.status !== 'accepted') throw new Error(`Transfer ${transfer.status}`);
      status('phone-status', 'Approved. Uploading...'); await json(`/api/transfers/${offer.transfer_id}/upload`, {method: 'POST', headers: {'content-type': file.type || 'application/octet-stream'}, body: file}); status('phone-status', 'Transfer complete.');
    } catch (error) { status('phone-status', error.message, true); event.target.querySelector('button').disabled = false; }
  });

  const incoming = document.querySelector('[data-transfer-id]');
  if (incoming) document.querySelectorAll('[data-transfer-action]').forEach((button) => button.addEventListener('click', async () => {
    document.querySelectorAll('[data-transfer-action]').forEach((item) => { item.disabled = true; });
    try { const data = await json(`/api/transfers/${incoming.dataset.transferId}/${button.dataset.transferAction}`, {method: 'POST'}); status('incoming-status', `Transfer ${data.status}.`); } catch (error) { status('incoming-status', error.message, true); }
  }));

  const historyList = document.getElementById('history-list');
  if (historyList) { json('/api/history').then((data) => { historyList.innerHTML = data.transfers.length ? data.transfers.map((item) => `<div class="history-row"><span><strong>${escapeHtml(item.filename)}</strong><small class="muted">${escapeHtml(item.direction)} · ${formatSize(item.size)}</small></span><span class="pill">${escapeHtml(item.status)}</span></div>`).join('') : '<p class="muted">No transfers yet.</p>'; }).catch((error) => { historyList.innerHTML = `<p class="status error">${escapeHtml(error.message)}</p>`; }); }

  const settings = document.querySelector('[data-settings-form]');
  if (settings) settings.addEventListener('submit', async (event) => { event.preventDefault(); const form = new FormData(event.target); try { await json('/api/settings', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(Object.fromEntries(form.entries()))}); status('settings-status', 'Settings saved.'); } catch (error) { status('settings-status', error.message, true); } });
})();
