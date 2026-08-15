import React, { useState, useEffect } from 'react';
import { View, StyleSheet, FlatList, TouchableOpacity } from 'react-native';
import { Text, Card, ActivityIndicator, useTheme, Button } from 'react-native-paper';
import client from '../api/client';

export default function TemplateScreen({ navigation, route }) {
    const { url } = route.params;
    const [templates, setTemplates] = useState([]);
    const [loading, setLoading] = useState(true);
    const theme = useTheme();

    useEffect(() => {
        fetchTemplates();
    }, []);

    const fetchTemplates = async () => {
        try {
            const response = await client.get('/api/v1/templates');
            setTemplates(response.data);
        } catch (error) {
            console.error(error);
        } finally {
            setLoading(false);
        }
    };

    const handleSelect = (templateId) => {
        navigation.navigate('Processing', { url, templateId });
    };

    const renderItem = ({ item }) => (
        <Card style={styles.card} onPress={() => handleSelect(item.id)}>
            <Card.Content>
                <Text variant="titleMedium">{item.name || item.id}</Text>
                <Text variant="bodySmall" style={{ opacity: 0.7 }}>{item.description || 'No description'}</Text>
            </Card.Content>
            <Card.Actions>
                <Button onPress={() => handleSelect(item.id)}>Select</Button>
            </Card.Actions>
        </Card>
    );

    if (loading) {
        return (
            <View style={[styles.container, styles.centered, { backgroundColor: theme.colors.background }]}>
                <ActivityIndicator size="large" />
                <Text style={{ marginTop: 20 }}>Loading templates...</Text>
            </View>
        );
    }

    return (
        <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
            <Text variant="headlineSmall" style={styles.header}>Select a Template</Text>
            <FlatList
                data={templates}
                renderItem={renderItem}
                keyExtractor={(item) => item.id}
                contentContainerStyle={styles.list}
            />
        </View>
    );
}

const styles = StyleSheet.create({
    container: {
        flex: 1,
        padding: 16,
    },
    centered: {
        justifyContent: 'center',
        alignItems: 'center',
    },
    header: {
        marginBottom: 16,
        fontWeight: 'bold',
    },
    list: {
        paddingBottom: 20,
    },
    card: {
        marginBottom: 12,
    },
});
