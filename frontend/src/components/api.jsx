import axios from 'axios';

const API_URL = "http://localhost:8000/api";

export const generateShort = async (video_url) => {
    const response = await axios.post(`${API_URL}/generate-short`, { url:video_url });
    return response.data;
}