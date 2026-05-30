import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getBaseCurrency,
  setBaseCurrency,
  syncEcbRates,
  getExchangeRates,
  setExchangeRate,
} from "../api/tauri";
import Alert from "../components/Alert";

const CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF", "SEK", "NOK", "DKK", "MXN"];

export default function Options() {
  const { t } = useTranslation();
  const [baseCurrency, setBaseCurrencyState] = useState("USD");
  const [rates, setRates] = useState([]);
  const [drafts, setDrafts] = useState({});
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [alert, setAlert] = useState({ type: "", msg: "" });

  const load = async (targetCurrency) => {
    try {
      setLoading(true);
      const base = targetCurrency || await getBaseCurrency();
      const rateRows = await getExchangeRates(base);
      setBaseCurrencyState(base);
      setRates(rateRows);
      setDrafts(
        rateRows.reduce((acc, row) => {
          acc[`${row.from_currency}->${row.to_currency}`] = String(row.rate);
          return acc;
        }, {})
      );
    } catch (e) {
      setAlert({ type: "danger", msg: String(e) });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const handleBaseCurrencyChange = async (nextBase) => {
    try {
      await setBaseCurrency(nextBase);
      setAlert({ type: "success", msg: `${t("options.baseCurrency")} ${nextBase}` });
      await load(nextBase);
    } catch (e) {
      setAlert({ type: "danger", msg: String(e) });
    }
  };

  const handleSync = async () => {
    try {
      setSyncing(true);
      const result = await syncEcbRates();
      setAlert({
        type: "success",
        msg: `${t("options.syncNow")}: ${result.synced_pairs} ${t("options.rates").toLowerCase()} (${result.base_currency})`,
      });
      await load(result.base_currency);
    } catch (e) {
      setAlert({ type: "danger", msg: String(e) });
    } finally {
      setSyncing(false);
    }
  };

  const handleSaveRate = async (fromCurrency, toCurrency) => {
    const key = `${fromCurrency}->${toCurrency}`;
    const parsed = Number(drafts[key]);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      setAlert({ type: "danger", msg: t("options.invalidRate") });
      return;
    }

    try {
      await setExchangeRate(fromCurrency, toCurrency, parsed);
      setAlert({ type: "success", msg: `${fromCurrency} -> ${toCurrency} ${t("common.save").toLowerCase()}` });
      await load(toCurrency);
    } catch (e) {
      setAlert({ type: "danger", msg: String(e) });
    }
  };

  const rows = useMemo(() => rates, [rates]);

  return (
    <div>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <h4 className="fw-bold mb-0">{t("options.title")}</h4>
        <button className="btn btn-outline-primary" onClick={handleSync} disabled={syncing || loading}>
          <i className={`bi ${syncing ? "bi-arrow-repeat" : "bi-cloud-download"} me-1`} />
          {syncing ? t("common.loading") : t("options.syncNow")}
        </button>
      </div>

      <Alert type={alert.type} message={alert.msg} onDismiss={() => setAlert({ type: "", msg: "" })} />

      <div className="card mb-4">
        <div className="card-body">
          <div className="row g-3 align-items-end">
            <div className="col-md-4">
              <label className="form-label">{t("options.baseCurrency")}</label>
              <select
                className="form-select"
                value={baseCurrency}
                onChange={(e) => handleBaseCurrencyChange(e.target.value)}
              >
                {CURRENCIES.map((currency) => (
                  <option key={currency} value={currency}>{currency}</option>
                ))}
              </select>
            </div>
            <div className="col-md-8 text-muted small">
              {t("options.autoSyncHint")}
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header d-flex justify-content-between align-items-center">
          <span>{t("options.rates")} ({baseCurrency})</span>
          <small className="text-muted">{rows.length} {t("options.rates").toLowerCase()}</small>
        </div>
        <div className="table-responsive">
          <table className="table table-sm table-hover mb-0">
            <thead className="table-light">
              <tr>
                <th>{t("options.fromCurrency")}</th>
                <th>{t("options.toCurrency")}</th>
                <th>{t("options.rate")}</th>
                <th>{t("options.provider")}</th>
                <th>{t("options.updatedAt")}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={6} className="text-center py-4 text-muted">{t("common.loading")}</td></tr>
              ) : rows.length === 0 ? (
                <tr><td colSpan={6} className="text-center py-4 text-muted">{t("common.noData")}</td></tr>
              ) : (
                rows.map((row) => {
                  const key = `${row.from_currency}->${row.to_currency}`;
                  const isIdentity = row.from_currency === row.to_currency;
                  return (
                    <tr key={key}>
                      <td className="fw-semibold">{row.from_currency}</td>
                      <td>{row.to_currency}</td>
                      <td style={{ minWidth: 160 }}>
                        <input
                          className="form-control form-control-sm"
                          type="number"
                          step="0.000001"
                          min="0"
                          value={drafts[key] ?? ""}
                          disabled={isIdentity}
                          onChange={(e) => setDrafts({ ...drafts, [key]: e.target.value })}
                        />
                      </td>
                      <td><span className="badge bg-secondary text-uppercase">{row.provider}</span></td>
                      <td className="text-muted small">{row.updated_at}</td>
                      <td>
                        <button
                          className="btn btn-sm btn-primary"
                          disabled={isIdentity}
                          onClick={() => handleSaveRate(row.from_currency, row.to_currency)}
                        >
                          {t("common.save")}
                        </button>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
