import React, { useState } from 'react';
import { View, StyleSheet } from 'react-native';
import { TextInput, Button, Text, Surface, useTheme, HelperText } from 'react-native-paper';

export default function HomeScreen({ navigation }) {
    const [url, setUrl] = useState('');
    const [error, setError] = useState('');
    const theme = useTheme();

    const handleNext = () => {
        if (!url.includes('instagram.com') && !url.includes('instagr.am')) {
            setError('Please enter a valid Instagram URL');
            return;
        }
        setError('');
        navigation.navigate('Template', { url });
    };

    return (
        <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
            <Surface style={styles.surface} elevation={2}>
                <Text variant="headlineMedium" style={styles.title}>
                    New Project
                </Text>
                <Text variant="bodyMedium" style={styles.subtitle}>
                    Paste an Instagram Reel URL to get started.
                </Text>

                <TextInput
                    label="Instagram URL"
                    value={url}
                    onChangeText={(text) => {
                        setUrl(text);
                        setError('');
                    }}
                    mode="outlined"
                    style={styles.input}
                    autoCapitalize="none"
                    placeholder="https://www.instagram.com/reel/..."
                    error={!!error}
                />
                <HelperText type="error" visible={!!error}>
                    {error}
                </HelperText>

                <Button
                    mode="contained"
                    onPress={handleNext}
                    style={styles.button}
                    disabled={!url}
                >
                    Next
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
        marginBottom: 5,
    },
    button: {
        width: '100%',
        marginTop: 10,
        paddingVertical: 6,
    },
});
