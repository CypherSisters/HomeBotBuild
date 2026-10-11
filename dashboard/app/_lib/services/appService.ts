import { apiGet, apiPut, apiPost } from '../api';

export type AppName = 'alarmflow' | 'calendar' | 'freshtable' | 'fitfam' | 'trivia';

export async function getAppData(appName: AppName): Promise<any> {
  return apiGet(`/apps/${appName}/data`);
}

export async function setAppData(appName: AppName, data: any): Promise<{ success: boolean }> {
  return apiPut(`/apps/${appName}/data`, data);
}

export async function appAction(appName: AppName, action: string, params?: any): Promise<any> {
  return apiPost(`/apps/${appName}/action`, { action, params: params || {} });
}

export async function getAppUpdateStatus(appName: AppName): Promise<{ last_updated: string }> {
  return apiGet(`/apps/${appName}/updated`);
}

/**
 * Calendar occurrences for a date window, `from`/`to` inclusive as YYYY-MM-DD.
 *
 * The window is capped at 31 days by the backend. A recurring event is one
 * stored row and N occurrences here, which is what the week view renders — see
 * app/_lib/calendarWeek.ts for the fallback when a box predates this route.
 */
export async function getCalendarOccurrences(
  from: string,
  to: string,
): Promise<{ from: string; to: string; occurrences: any[] }> {
  const q = `from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
  return apiGet(`/apps/calendar/occurrences?${q}`);
}
