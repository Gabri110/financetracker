import { invoke } from "@tauri-apps/api/core";

// DB lifecycle
export const openDb = (path) => invoke("open_db", { path });
export const createDb = (path) => invoke("create_db", { path });
export const getDbPath = () => invoke("get_db_path");
export const closeDb = () => invoke("close_db");
export const openDemoDb = () => invoke("open_demo_db");

// Accounts
export const getAccounts = () => invoke("get_accounts");
export const addAccount = (name, accountType, balance, currency) =>
  invoke("add_account", { name, accountType, balance, currency });
export const updateAccount = (id, name, accountType, balance, currency) =>
  invoke("update_account", { id, name, accountType, balance, currency });
export const adjustAccountBalance = (id, targetBalance) =>
  invoke("adjust_account_balance", { id, targetBalance });
export const deleteAccount = (id) => invoke("delete_account", { id });

// Transactions
export const getTransactions = (filter) => invoke("get_transactions", { filter: filter ?? null });
export const addTransaction = (data) => invoke("add_transaction", data);
export const updateTransaction = (data) => invoke("update_transaction", data);
export const deleteTransaction = (id) => invoke("delete_transaction", { id });
export const importCsv = (data) => invoke("import_csv", data);
export const exportTransactionsCsv = (filter) =>
  invoke("export_transactions_csv", { filter: filter ?? null });
export const importTransactionsCanonicalCsv = (csvContent) =>
  invoke("import_transactions_canonical_csv", { csvContent });

// Categories
export const getCategories = () => invoke("get_categories");
export const addCategory = (name, categoryType, icon, color) =>
  invoke("add_category", { name, categoryType, icon, color });
export const updateCategory = (id, name, categoryType, icon, color) =>
  invoke("update_category", { id, name, categoryType, icon, color });
export const deleteCategory = (id) => invoke("delete_category", { id });
export const exportCategoriesCsv = () => invoke("export_categories_csv");
export const importCategoriesCsv = (csvContent) =>
  invoke("import_categories_csv", { csvContent });

// Budgets
export const getBudgets = () => invoke("get_budgets");
export const addBudget = (categoryId, amount, period, startDate) =>
  invoke("add_budget", { categoryId, amount, period, startDate });
export const updateBudget = (id, categoryId, amount, period, startDate) =>
  invoke("update_budget", { id, categoryId, amount, period, startDate });
export const deleteBudget = (id) => invoke("delete_budget", { id });

// Rules
export const getRules = () => invoke("get_rules");
export const addRule = (categoryId, pattern, matchField, isRegex, priority) =>
  invoke("add_rule", { categoryId, pattern, matchField, isRegex, priority });
export const updateRule = (id, categoryId, pattern, matchField, isRegex, priority) =>
  invoke("update_rule", { id, categoryId, pattern, matchField, isRegex, priority });
export const deleteRule = (id) => invoke("delete_rule", { id });
export const autoCategorize = () => invoke("auto_categorize");
export const exportRulesCsv = () => invoke("export_rules_csv");
export const importRulesCsv = (csvContent) =>
  invoke("import_rules_csv", { csvContent });

// Full backup (base64 payloads)
export const exportFullBackupZipBase64 = () => invoke("export_full_backup_zip_base64");
export const exportFullBackupXlsxBase64 = () => invoke("export_full_backup_xlsx_base64");
export const importFullBackupZipBase64 = (archiveBase64) =>
  invoke("import_full_backup_zip_base64", { archiveBase64 });
export const importFullBackupXlsxBase64 = (workbookBase64) =>
  invoke("import_full_backup_xlsx_base64", { workbookBase64 });

// FX / settings
export const getBaseCurrency = () => invoke("get_base_currency");
export const setBaseCurrency = (currency) => invoke("set_base_currency", { currency });
export const syncEcbRates = () => invoke("sync_ecb_rates");
export const getExchangeRates = (toCurrency) =>
  invoke("get_exchange_rates", { toCurrency: toCurrency ?? null });
export const setExchangeRate = (fromCurrency, toCurrency, rate) =>
  invoke("set_exchange_rate", { fromCurrency, toCurrency, rate });

// Reports
export const getDashboardStats = () => invoke("get_dashboard_stats");
export const getMonthlyData = (months) => invoke("get_monthly_data", { months });
export const getCategorySpending = (period) => invoke("get_category_spending", { period });
export const getTopPayees = (limit) => invoke("get_top_payees", { limit });
export const getRecentTransactions = (limit) => invoke("get_recent_transactions", { limit });
