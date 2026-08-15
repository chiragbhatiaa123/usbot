import { initializeApp } from "firebase/app";
import { getFirestore } from "firebase/firestore";

const firebaseConfig = {
    apiKey: "AIzaSyAzuUpRPb-FIpbhMB-C6wSWf5_T8H9yH80",
    authDomain: "templatea-queue.firebaseapp.com",
    projectId: "templatea-queue",
    storageBucket: "templatea-queue.firebasestorage.app",
    messagingSenderId: "645047736070",
    appId: "1:645047736070:web:3e09cf47149ac362c67087"
};

const app = initializeApp(firebaseConfig);
export const db = getFirestore(app);
