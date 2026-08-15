import React, { useState, useEffect } from 'react';
import { View, StyleSheet, ScrollView } from 'react-native';
import { Text, Button, Card, TextInput, useTheme, ActivityIndicator, RadioButton } from 'react-native-paper';
import client from '../api/client';

export default function CopySelectionScreen({ navigation, route }) {
    const { workspaceId } = route.params;
    const [loading, setLoading] = useState(true);
    const [ocrText, setOcrText] = useState('');
    const [aiCopies, setAiCopies] = useState([]);
    const [selectedOption, setSelectedOption] = useState('ocr'); // 'ocr', 'manual', or index of ai
    const [manualText, setManualText] = useState('');
    const theme = useTheme();

    useEffect(() => {
        fetchOptions();
    }, []);

    const fetchOptions = async () => {
        try {
            const response = await client.get(`/api/v1/workspaces/${workspaceId}`);
            const files = response.data.files || {};

            // Fetch OCR
            if (files.ocr && files.ocr.url) {
                try {
                    const ocrRes = await client.get(files.ocr.url);
                    setOcrText(typeof ocrRes.data === 'string' ? ocrRes.data : JSON.stringify(ocrRes.data));
                } catch (e) { console.log('Failed to fetch OCR text'); }
            }

            // Fetch AI Copies
            if (files.ai_copies && files.ai_copies.url) {
                try {
                    const aiRes = await client.get(files.ai_copies.url);
                    setAiCopies(Array.isArray(aiRes.data) ? aiRes.data : []);
                } catch (e) { console.log('Failed to fetch AI copies'); }
            }
        } catch (error) {
            console.error(error);
        } finally {
            setLoading(false);
        }
    };

    const handleSubmit = async () => {
        let finalText = '';
        let type = 'manual';

        if (selectedOption === 'manual') {
            finalText = manualText;
            type = 'manual';
        } else if (selectedOption === 'ocr') {
            finalText = ocrText;
            type = 'ocr';
        } else {
            // AI option
            const index = parseInt(selectedOption);
            const copy = aiCopies[index];
            finalText = typeof copy === 'string' ? copy : copy.text;
            type = 'ai';
        }

        if (!finalText) {
            alert('Please select or enter some text.');
            return;
        }

        try {
            setLoading(true);
            await client.post(`/api/v1/workspaces/${workspaceId}/choice`, {
                type,
                text: finalText,
            });
            navigation.replace('Result', { workspaceId, selectedText: finalText });
        } catch (error) {
            console.error(error);
            setLoading(false);
            alert('Failed to submit choice.');
        }
    };

    if (loading) {
        return (
            <View style={[styles.container, styles.centered, { backgroundColor: theme.colors.background }]}>
                <ActivityIndicator size="large" />
                <Text style={{ marginTop: 20 }}>Loading options...</Text>
            </View>
        );
    }

    return (
        <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
            <ScrollView contentContainerStyle={styles.scroll}>
                <Text variant="headlineSmall" style={styles.header}>Select Copy</Text>

                <RadioButton.Group onValueChange={value => setSelectedOption(value)} value={selectedOption}>

                    {/* OCR Option */}
                    {ocrText ? (
                        <Card style={styles.card} onPress={() => setSelectedOption('ocr')}>
                            <Card.Content style={styles.cardContent}>
                                <RadioButton value="ocr" />
                                <View style={styles.textContainer}>
                                    <Text variant="titleMedium">Original (OCR)</Text>
                                    <Text variant="bodyMedium" numberOfLines={3}>{ocrText}</Text>
                                </View>
                            </Card.Content>
                        </Card>
                    ) : null}

                    {/* AI Options */}
                    {aiCopies.map((copy, index) => {
                        const text = typeof copy === 'string' ? copy : copy.text;
                        return (
                            <Card key={index} style={styles.card} onPress={() => setSelectedOption(index.toString())}>
                                <Card.Content style={styles.cardContent}>
                                    <RadioButton value={index.toString()} />
                                    <View style={styles.textContainer}>
                                        <Text variant="titleMedium">AI Suggestion {index + 1}</Text>
                                        <Text variant="bodyMedium" numberOfLines={3}>{text}</Text>
                                    </View>
                                </Card.Content>
                            </Card>
                        );
                    })}

                    {/* Manual Option */}
                    <Card style={styles.card} onPress={() => setSelectedOption('manual')}>
                        <Card.Content style={styles.cardContent}>
                            <RadioButton value="manual" />
                            <View style={styles.textContainer}>
                                <Text variant="titleMedium">Manual Edit</Text>
                            </View>
                        </Card.Content>
                        {selectedOption === 'manual' && (
                            <Card.Content>
                                <TextInput
                                    mode="outlined"
                                    multiline
                                    numberOfLines={4}
                                    value={manualText}
                                    onChangeText={setManualText}
                                    placeholder="Type your copy here..."
                                />
                            </Card.Content>
                        )}
                    </Card>

                </RadioButton.Group>
            </ScrollView>

            <View style={styles.footer}>
                <Button mode="contained" onPress={handleSubmit} style={styles.button}>
                    Render Video
                </Button>
            </View>
        </View>
    );
}

const styles = StyleSheet.create({
    container: {
        flex: 1,
    },
    centered: {
        justifyContent: 'center',
        alignItems: 'center',
    },
    scroll: {
        padding: 16,
        paddingBottom: 80,
    },
    header: {
        marginBottom: 16,
        fontWeight: 'bold',
    },
    card: {
        marginBottom: 12,
    },
    cardContent: {
        flexDirection: 'row',
        alignItems: 'center',
    },
    textContainer: {
        flex: 1,
        marginLeft: 10,
    },
    footer: {
        position: 'absolute',
        bottom: 0,
        left: 0,
        right: 0,
        padding: 16,
        backgroundColor: 'white',
        elevation: 4,
    },
    button: {
        paddingVertical: 6,
    },
});
