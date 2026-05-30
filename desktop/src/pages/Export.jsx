import { useState } from "react";
import Alert from "../components/Alert";
import {
  exportTransactionsCsv,
  exportCategoriesCsv,
  exportRulesCsv,
  importTransactionsCanonicalCsv,
  importCategoriesCsv,
  importRulesCsv,
  exportFullBackupZipBase64,
  exportFullBackupXlsxBase64,
  importFullBackupZipBase64,
  importFullBackupXlsxBase64,
} from "../api/tauri";

function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function downloadBinary(filename, bytes, mimeType) {
  const blob = new Blob([bytes], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

export default function ExportPage() {
  const [alert, setAlert] = useState({ type: "", msg: "" });
  const [busy, setBusy] = useState(false);

  const exportDataset = async (kind) => {
    setBusy(true);
    try {
      const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
      if (kind === "transactions") {
        const csv = await exportTransactionsCsv(null);
        downloadText(`transactions_${timestamp}.csv`, csv);
      } else if (kind === "categories") {
        const csv = await exportCategoriesCsv();
        downloadText(`categories_${timestamp}.csv`, csv);
      } else if (kind === "rules") {
        const csv = await exportRulesCsv();
        downloadText(`rules_${timestamp}.csv`, csv);
      } else if (kind === "full-zip") {
        const payload = await exportFullBackupZipBase64();
        downloadBinary(
          `financetracker_backup_${timestamp}.zip`,
          base64ToBytes(payload),
          "application/zip"
        );
      } else if (kind === "full-xlsx") {
        const payload = await exportFullBackupXlsxBase64();
        downloadBinary(
          `financetracker_backup_${timestamp}.xlsx`,
          base64ToBytes(payload),
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        );
      }
      setAlert({ type: "success", msg: "Export completed." });
    } catch (e) {
      setAlert({ type: "danger", msg: String(e) });
    } finally {
      setBusy(false);
    }
  };

  const readFileText = (file) =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("Failed to read file"));
      reader.readAsText(file);
    });

  const importDataset = async (event, kind) => {
    const file = event.target.files?.[0];
    if (!file) return;

    setBusy(true);
    try {
      const content = await readFileText(file);
      let result;
      if (kind === "transactions") {
        result = await importTransactionsCanonicalCsv(content);
      } else if (kind === "categories") {
        result = await importCategoriesCsv(content);
      } else {
        result = await importRulesCsv(content);
      }

      const parts = [];
      if (typeof result.imported === "number") parts.push(`imported: ${result.imported}`);
      if (typeof result.inserted === "number") parts.push(`inserted: ${result.inserted}`);
      if (typeof result.updated === "number") parts.push(`updated: ${result.updated}`);
      if (typeof result.skipped === "number") parts.push(`skipped: ${result.skipped}`);
      setAlert({ type: "success", msg: `Import completed (${parts.join(", ")}).` });
    } catch (e) {
      setAlert({ type: "danger", msg: String(e) });
    } finally {
      event.target.value = "";
      setBusy(false);
    }
  };

  const importFullBackup = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    setBusy(true);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const payload = bytesToBase64(bytes);
      let result;

      if (file.name.toLowerCase().endsWith(".zip")) {
        result = await importFullBackupZipBase64(payload);
      } else if (file.name.toLowerCase().endsWith(".xlsx")) {
        result = await importFullBackupXlsxBase64(payload);
      } else {
        throw new Error("Unsupported file type. Use .zip or .xlsx");
      }

      setAlert({
        type: "success",
        msg:
          "Full restore completed (" +
          `accounts +${result.accountsInserted || result.accounts_inserted || 0}, ` +
          `categories +${result.categoriesInserted || result.categories_inserted || 0}, ` +
          `rules +${result.rulesInserted || result.rules_inserted || 0}, ` +
          `transactions +${result.transactionsImported || result.transactions_imported || 0}` +
          ").",
      });
    } catch (e) {
      setAlert({ type: "danger", msg: String(e) });
    } finally {
      event.target.value = "";
      setBusy(false);
    }
  };

  return (
    <div>
      <h4 className="fw-bold mb-4">Export & Restore</h4>
      <Alert type={alert.type} message={alert.msg} onDismiss={() => setAlert({ type: "", msg: "" })} />

      <div className="card mb-4">
        <div className="card-body">
          <h6 className="fw-semibold mb-3">Export CSV</h6>
          <div className="d-flex flex-wrap gap-2">
            <button className="btn btn-outline-primary" disabled={busy} onClick={() => exportDataset("transactions")}>
              Export Transactions CSV
            </button>
            <button className="btn btn-outline-primary" disabled={busy} onClick={() => exportDataset("categories")}>
              Export Categories CSV
            </button>
            <button className="btn btn-outline-primary" disabled={busy} onClick={() => exportDataset("rules")}>
              Export Rules CSV
            </button>
            <button className="btn btn-outline-success" disabled={busy} onClick={() => exportDataset("full-zip")}>
              Export Full Backup ZIP
            </button>
            <button className="btn btn-outline-success" disabled={busy} onClick={() => exportDataset("full-xlsx")}>
              Export Full Backup XLSX
            </button>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-body">
          <h6 className="fw-semibold mb-3">Restore from CSV (merge/upsert)</h6>
          <div className="row g-3">
            <div className="col-md-4">
              <label className="form-label">Transactions CSV</label>
              <input className="form-control" type="file" accept=".csv" disabled={busy} onChange={(e) => importDataset(e, "transactions")} />
            </div>
            <div className="col-md-4">
              <label className="form-label">Categories CSV</label>
              <input className="form-control" type="file" accept=".csv" disabled={busy} onChange={(e) => importDataset(e, "categories")} />
            </div>
            <div className="col-md-4">
              <label className="form-label">Rules CSV</label>
              <input className="form-control" type="file" accept=".csv" disabled={busy} onChange={(e) => importDataset(e, "rules")} />
            </div>
          </div>
        </div>
      </div>

      <div className="card mt-4">
        <div className="card-body">
          <h6 className="fw-semibold mb-3">Restore full backup (ZIP or XLSX)</h6>
          <input
            className="form-control"
            type="file"
            accept=".zip,.xlsx"
            disabled={busy}
            onChange={importFullBackup}
          />
        </div>
      </div>
    </div>
  );
}
