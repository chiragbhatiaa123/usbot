import axios from 'axios';
import { Alert } from 'react-native';

// Default to localhost for emulator, but this will be overridden by the user
let BASE_URL = 'http://10.144.75.184:8000';
let API_KEY = '';

const client = axios.create({
  baseURL: BASE_URL,
  timeout: 30000, // 30 seconds
  headers: {
    'Content-Type': 'application/json',
  },
});

export const setBaseUrl = (url) => {
  BASE_URL = url.replace(/\/$/, ''); // Remove trailing slash
  client.defaults.baseURL = BASE_URL;
};

export const setApiKey = (key) => {
  API_KEY = key;
};

export const getBaseUrl = () => BASE_URL;
export const getApiKey = () => API_KEY;

// Add a request interceptor to inject the API Key
client.interceptors.request.use((config) => {
  if (API_KEY) {
    config.headers['X-API-Key'] = API_KEY;
  }
  return config;
});


// Add a response interceptor to handle common errors
client.interceptors.response.use(
  (response) => response,
  (error) => {
    console.error('API Error:', error);
    if (error.code === 'ECONNABORTED') {
      Alert.alert('Connection Error', 'Request timed out. Please check your network.');
    } else if (!error.response) {
      Alert.alert('Network Error', 'Could not connect to server. Check your IP settings.');
    }
    return Promise.reject(error);
  }
);

export default client;
