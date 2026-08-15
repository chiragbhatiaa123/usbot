import React, { useState, useEffect } from 'react';
import { View, StyleSheet } from 'react-native';
import { TextInput, Button, Text, Surface, useTheme } from 'react-native-paper';
import { setBaseUrl, getBaseUrl, setApiKey, getApiKey } from '../api/client';
import client from '../api/client';

export default function ConnectScreen({ navigation }) {
    const [url, setUrl] = useState(getBaseUrl());
    const [apiKey, setApiKeyInput] = useState(getApiKey());
    const [loading, setLoading] = useState(false);
    const theme = useTheme();

    const handleConnect = async () => {
        setLoading(true);
        setBaseUrl(url);
        setApiKey(apiKey);
        try {
            // Simple health check or just list templates to verify connection
            await client.get('/api/v1/templates');
            navigation.replace('Home');
        } catch (error) {
            // Error is handled by interceptor, but we can show specific message here if needed
            console.log('Connection failed');
        } finally {
            setLoading(false);
        }
    };

    return (
        <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
            <Surface style={styles.surface} elevation={4}>
                <Text variant="headlineMedium" style={styles.title}>
                    Connect to Server
                </Text>
                <Text variant="bodyMedium" style={styles.subtitle}>
                    Enter the local IP address of your backend server.
                </Text>

                <TextInput
                    label="Server URL"
                    value={url}
                    onChangeText={setUrl}
                    mode="outlined"
                    style={styles.input}
                    autoCapitalize="none"
                    keyboardType="url"
                    placeholder="http://192.168.1.x:8000"
                />

                <TextInput
                    label="API Key (Optional)"
                    value={apiKey}
                    onChangeText={setApiKeyInput}
                    mode="outlined"
                    style={styles.input}
                    autoCapitalize="none"
                    secureTextEntry
                    placeholder="Enter API Key if required"
                />

                <Button
                    mode="contained"
                    onPress={handleConnect}
                    loading={loading}
                    style={styles.button}
                >
                    Connect
                </Button>
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
        padding: 20,
        borderRadius: 8,
        alignItems: 'center',
    },
    title: {
        marginBottom: 10,
        fontWeight: 'bold',
    },
    subtitle: {
        marginBottom: 20,
        textAlign: 'center',
        opacity: 0.7,
    },
    input: {
        width: '100%',
        marginBottom: 20,
    },
    button: {
        width: '100%',
        paddingVertical: 6,
    },
});
