import React, { useState, useEffect, useRef } from 'react';
import { View, StyleSheet, Alert } from 'react-native';
import { Text, ActivityIndicator, Button, useTheme, Surface } from 'react-native-paper';
import { Video, ResizeMode } from 'expo-av';
import * as FileSystem from 'expo-file-system/legacy';
import * as Sharing from 'expo-sharing';
import client, { getBaseUrl } from '../api/client';

export default function ResultScreen({ navigation, route }) {
    const { workspaceId } = route.params;
    const [status, setStatus] = useState('Rendering...');
    const [videoUrl, setVideoUrl] = useState(null);
    const [downloading, setDownloading] = useState(false);
    const theme = useTheme();
    const pollingRef = useRef(null);
    const videoRef = useRef(null);

    useEffect(() => {
        startPolling();
        return () => stopPolling();
    }, []);

    const startPolling = () => {
        pollingRef.current = setInterval(async () => {
            try {
                const response = await client.get(`/api/v1/workspaces/${workspaceId}`);
                const ws = response.data;
                const wsStatus = ws.status || {};
                const renderStatus = wsStatus['04_render'];

                if (renderStatus === 'running') {
                    setStatus('Rendering video...');
                } else if (renderStatus === 'success') {
                    stopPolling();
                    setStatus('Render Complete!');

                    // Get video URL
                    if (ws.files && ws.files.final && ws.files.final.url) {
                        // Construct full URL
                        const relativeUrl = ws.files.final.url;
                        const fullUrl = `${getBaseUrl()}${relativeUrl}`;
                        setVideoUrl(fullUrl);
                    }
                } else if (renderStatus === 'failed') {
                    stopPolling();
                    setStatus('Rendering Failed.');
                }
            } catch (error) {
                console.error('Polling error', error);
            }
        }, 2000);
    };

    const stopPolling = () => {
        if (pollingRef.current) {
            clearInterval(pollingRef.current);
            pollingRef.current = null;
        }
    };

    const handleDownload = async () => {
        if (!videoUrl) return;

        setDownloading(true);
        try {
            const filename = `video_${workspaceId}.mp4`;
            const fileUri = FileSystem.documentDirectory + filename;

            const downloadRes = await FileSystem.downloadAsync(videoUrl, fileUri);

            if (await Sharing.isAvailableAsync()) {
                await Sharing.shareAsync(downloadRes.uri);
            } else {
                Alert.alert('Saved', `Video saved to ${downloadRes.uri}`);
            }
        } catch (error) {
            console.error(error);
            Alert.alert('Error', 'Failed to download video');
        } finally {
            setDownloading(false);
        }
    };

    const handleHome = () => {
        navigation.popToTop();
    };

    return (
        <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
            {videoUrl ? (
                <View style={styles.content}>
                    <Text variant="headlineSmall" style={styles.title}>Your Video is Ready!</Text>

                    <View style={styles.videoContainer}>
                        <Video
                            ref={videoRef}
                            style={styles.video}
                            source={{ uri: videoUrl }}
                            useNativeControls
                            resizeMode={ResizeMode.CONTAIN}
                            isLooping
                            shouldPlay
                        />
                    </View>

                    <Button
                        mode="contained"
                        icon="download"
                        onPress={handleDownload}
                        loading={downloading}
                        style={styles.button}
                    >
                        Download / Share
                    </Button>

                    <Button mode="outlined" onPress={handleHome} style={styles.button}>
                        Create Another
                    </Button>
                </View>
            ) : (
                <Surface style={styles.surface} elevation={2}>
                    <Text variant="headlineSmall" style={styles.title}>Rendering</Text>
                    <ActivityIndicator animating={true} size="large" style={styles.loader} />
                    <Text variant="bodyLarge">{status}</Text>
                    <Text variant="bodySmall" style={styles.hint}>
                        This usually takes 1-2 minutes.
                    </Text>
                </Surface>
            )}
        </View>
    );
}

const styles = StyleSheet.create({
    container: {
        flex: 1,
        justifyContent: 'center',
        padding: 20,
    },
    content: {
        flex: 1,
        alignItems: 'center',
        justifyContent: 'center',
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
    hint: {
        marginTop: 10,
        opacity: 0.6,
    },
    videoContainer: {
        width: '100%',
        height: 300,
        backgroundColor: 'black',
        borderRadius: 8,
        overflow: 'hidden',
        marginBottom: 20,
    },
    video: {
        flex: 1,
    },
    button: {
        width: '100%',
        marginBottom: 10,
        paddingVertical: 6,
    },
});
