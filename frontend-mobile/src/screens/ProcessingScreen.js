import React, { useState, useEffect, useRef } from 'react';
import { View, StyleSheet } from 'react-native';
import { Text, ActivityIndicator, ProgressBar, useTheme, Surface } from 'react-native-paper';
import client from '../api/client';

export default function ProcessingScreen({ navigation, route }) {
    const { url, templateId } = route.params;
    const [status, setStatus] = useState('Initializing...');
    const [progress, setProgress] = useState(0);
    const [workspaceId, setWorkspaceId] = useState(null);
    const theme = useTheme();
    const pollingRef = useRef(null);

    useEffect(() => {
        startProcessing();
        return () => stopPolling();
    }, []);

    const startProcessing = async () => {
        try {
            setStatus('Submitting job...');
            const response = await client.post('/api/v1/reels', {
                url,
                template_id: templateId,
                auto: false,
            });

            if (response.data && response.data.workspace) {
                const id = response.data.workspace.id;
                setWorkspaceId(id);
                startPolling(id);
            } else {
                setStatus('Failed to start processing.');
            }
        } catch (error) {
            console.error(error);
            setStatus('Error starting processing.');
        }
    };

    const startPolling = (id) => {
        pollingRef.current = setInterval(async () => {
            try {
                const response = await client.get(`/api/v1/workspaces/${id}`);
                const ws = response.data;
                const wsStatus = ws.status || {};

                // Update UI based on status
                // Update UI based on status
                // Check for success first (reverse order)
                if (wsStatus['02_ocr'] === 'success' || wsStatus['02_ocr'] === 'fallback_caption') {
                    setStatus('Generating AI copies...');
                    setProgress(0.8);
                    stopPolling();
                    navigation.replace('CopySelection', { workspaceId: id });
                } else if (wsStatus['02_ocr'] === 'running') {
                    setStatus('Extracting text (OCR)...');
                    setProgress(0.5);
                } else if (wsStatus['02_ocr'] === 'failed') {
                    stopPolling();
                    setStatus('OCR Failed. Please try again.');
                } else if (wsStatus['01_download'] === 'running' || wsStatus['00_raw'] === 'running') {
                    setStatus('Downloading video...');
                    setProgress(0.2);
                } else if (wsStatus['01_detector'] === 'running') {
                    setStatus('Detecting content...');
                    setProgress(0.3);
                }
            } catch (error) {
                console.error('Polling error', error);
            }
        }, 1000);
    };

    const stopPolling = () => {
        if (pollingRef.current) {
            clearInterval(pollingRef.current);
            pollingRef.current = null;
        }
    };

    return (
        <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
            <Surface style={styles.surface} elevation={2}>
                <Text variant="headlineSmall" style={styles.title}>Processing Reel</Text>

                <ActivityIndicator animating={true} size="large" style={styles.loader} />

                <Text variant="bodyLarge" style={styles.status}>{status}</Text>

                <ProgressBar progress={progress} style={styles.progress} />

                <Text variant="bodySmall" style={styles.hint}>
                    This usually takes 1-2 minutes. Please don't close the app.
                </Text>
            </Surface>
        </View>
    );
}

const styles = StyleSheet.create({
    container: {
        flex: 1,
        justifyContent: 'center',
        padding: 20,
    },
    surface: {
        padding: 30,
        borderRadius: 8,
        alignItems: 'center',
    },
    title: {
        marginBottom: 20,
        fontWeight: 'bold',
    },
    loader: {
        marginBottom: 20,
    },
    status: {
        marginBottom: 10,
        textAlign: 'center',
    },
    progress: {
        width: '100%',
        height: 8,
        borderRadius: 4,
        marginBottom: 20,
    },
    hint: {
        opacity: 0.6,
        textAlign: 'center',
    },
});
