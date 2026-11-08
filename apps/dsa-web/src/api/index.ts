import axios from 'axios';
import { API_BASE_URL } from '../utils/constants';
import { attachParsedApiError } from './error';
import { csrfHeadersFor, getUserCenterLoginUrl } from '../utils/userCenterSession';

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

// user_center mode: every non-GET request must carry the session's CSRF token.
apiClient.interceptors.request.use((config) => {
  Object.entries(csrfHeadersFor(config.method)).forEach(([name, value]) => {
    config.headers.set(name, value);
  });
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const userCenterLoginUrl = getUserCenterLoginUrl();
      if (userCenterLoginUrl) {
        // user_center mode: the session expired; sign in again via the user center.
        window.location.assign(userCenterLoginUrl);
        attachParsedApiError(error);
        return Promise.reject(error);
      }
      const path = window.location.pathname + window.location.search + window.location.hash;
      if (!path.startsWith('/login')) {
        const redirect = encodeURIComponent(path);
        window.location.assign(`/login?redirect=${redirect}`);
      }
    }
    attachParsedApiError(error);
    return Promise.reject(error);
  }
);

export default apiClient;
