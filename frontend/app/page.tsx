"use client";

import { useEffect, useState } from "react";
import styles from "./page.module.css";
import dashboardStyles from "@/components/dashboard.module.css";
import OverviewTab from "@/components/OverviewTab";
import AppointmentsTab from "@/components/AppointmentsTab";
import CustomersTab from "@/components/CustomersTab";
import NotificationsTab from "@/components/NotificationsTab";
import JobsTab from "@/components/JobsTab";
import EscalationsTab from "@/components/EscalationsTab";

const BUSINESS_ID_STORAGE_KEY = "dashboard.businessId";
const API_KEY_STORAGE_KEY = "dashboard.apiKey";

type Tab = "overview" | "appointments" | "customers" | "notifications" | "jobs" | "escalations";

const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "appointments", label: "Appointments" },
  { key: "customers", label: "Customers" },
  { key: "notifications", label: "Notifications" },
  { key: "jobs", label: "Jobs" },
  { key: "escalations", label: "Escalations" },
];

export default function DashboardPage() {
  const [businessId, setBusinessId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [activeTab, setActiveTab] = useState<Tab>("overview");

  useEffect(() => {
    // localStorage isn't available during server rendering, so stored
    // credentials have to be picked up client-side after mount rather
    // than as a useState initializer.
    const storedBusinessId = window.localStorage.getItem(BUSINESS_ID_STORAGE_KEY);
    const storedApiKey = window.localStorage.getItem(API_KEY_STORAGE_KEY);
    // Only the first setState call in an effect body needs the disable
    // comment -- the rule doesn't re-fire per call.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing from an external, client-only store, not a derived render value
    if (storedBusinessId) setBusinessId(storedBusinessId);
    if (storedApiKey) setApiKey(storedApiKey);
  }, []);

  useEffect(() => {
    if (!businessId.trim() || !apiKey.trim()) return;
    window.localStorage.setItem(BUSINESS_ID_STORAGE_KEY, businessId.trim());
    window.localStorage.setItem(API_KEY_STORAGE_KEY, apiKey.trim());
  }, [businessId, apiKey]);

  const ready = Boolean(businessId.trim() && apiKey.trim());
  const trimmedBusinessId = businessId.trim();
  const trimmedApiKey = apiKey.trim();

  return (
    <main className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Business Dashboard</h1>
        <p className={styles.subtitle}>
          Operator view over the appointment automation system -- the same lifecycle the customer
          chat drives, visible from the business side.
        </p>
      </div>

      <div className={styles.controls}>
        <div className={styles.field}>
          <label htmlFor="business-id">Business ID</label>
          <input
            id="business-id"
            type="text"
            placeholder="00000000-0000-0000-0000-000000000000"
            value={businessId}
            onChange={(e) => setBusinessId(e.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label htmlFor="api-key">API Key</label>
          <input
            id="api-key"
            type="password"
            placeholder="issued when the business was created"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </div>
      </div>

      {!ready && (
        <p className={styles.status}>Enter a business ID and its API key to load the dashboard.</p>
      )}

      {ready && (
        <>
          <div className={dashboardStyles.tabBar}>
            {TABS.map((tab) => (
              <button
                key={tab.key}
                type="button"
                className={`${dashboardStyles.tabButton} ${
                  activeTab === tab.key ? dashboardStyles.tabButtonActive : ""
                }`}
                onClick={() => setActiveTab(tab.key)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {activeTab === "overview" && (
            <OverviewTab businessId={trimmedBusinessId} apiKey={trimmedApiKey} />
          )}
          {activeTab === "appointments" && (
            <AppointmentsTab businessId={trimmedBusinessId} apiKey={trimmedApiKey} />
          )}
          {activeTab === "customers" && (
            <CustomersTab businessId={trimmedBusinessId} apiKey={trimmedApiKey} />
          )}
          {activeTab === "notifications" && (
            <NotificationsTab businessId={trimmedBusinessId} apiKey={trimmedApiKey} />
          )}
          {activeTab === "jobs" && <JobsTab businessId={trimmedBusinessId} apiKey={trimmedApiKey} />}
          {activeTab === "escalations" && (
            <EscalationsTab businessId={trimmedBusinessId} apiKey={trimmedApiKey} />
          )}
        </>
      )}
    </main>
  );
}
