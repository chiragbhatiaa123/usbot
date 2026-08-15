import React, { useState, useEffect } from 'react';
import { StyleSheet, Text, View, TextInput, TouchableOpacity, FlatList, Alert, ActivityIndicator, SafeAreaView, KeyboardAvoidingView, Platform, Linking } from 'react-native';
import { db } from './firebase';
import { collection, addDoc, query, orderBy, onSnapshot, serverTimestamp, limit } from 'firebase/firestore';
import * as SecureStore from 'expo-secure-store';
import { useShareIntent } from 'expo-share-intent';

export default function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [password, setPassword] = useState('');
  const [url, setUrl] = useState('');
  const [templateId, setTemplateId] = useState('');
  const [queue, setQueue] = useState([]);
  const [loading, setLoading] = useState(false);

  const { hasShareIntent, shareIntent, resetShareIntent } = useShareIntent();

  // Check auth on load
  useEffect(() => {
    checkAuth();
  }, []);

  // Handle Share Intent
  useEffect(() => {
    if (hasShareIntent && shareIntent.value) {
      // Instagram usually shares text like "Check out this reel... https://..."
      // We need to extract the URL
      const text = shareIntent.value;
      const urlMatch = text.match(/https?:\/\/[^\s]+/);
      if (urlMatch) {
        setUrl(urlMatch[0]);
      } else {
        setUrl(text); // Fallback
      }
    }
  }, [hasShareIntent, shareIntent]);

  const checkAuth = async () => {
    const auth = await SecureStore.getItemAsync('auth');
    if (auth === 'true') {
      setIsAuthenticated(true);
    }
  };

  const handleLogin = async () => {
    if (password === 'Taylor') {
      await SecureStore.setItemAsync('auth', 'true');
      setIsAuthenticated(true);
    } else {
      Alert.alert('Error', 'Incorrect password');
    }
  };

  const handleLogout = async () => {
    await SecureStore.deleteItemAsync('auth');
    setIsAuthenticated(false);
  };

  // Queue listener
  useEffect(() => {
    if (!isAuthenticated) return;

    const q = query(
      collection(db, 'queue'),
      orderBy('created_at', 'desc'),
      limit(20)
    );

    const unsubscribe = onSnapshot(q, (snapshot) => {
      const items = [];
      snapshot.forEach((doc) => {
        items.push({ id: doc.id, ...doc.data() });
      });
      setQueue(items);
    });

    return () => unsubscribe();
  }, [isAuthenticated]);

  const handleSubmit = async () => {
    if (!url || !templateId) {
      Alert.alert('Error', 'Please enter URL and Template ID');
      return;
    }

    setLoading(true);
    try {
      await addDoc(collection(db, 'queue'), {
        url,
        template_id: templateId,
        status: 'pending',
        created_at: serverTimestamp(),
        auto: true
      });
      setUrl('');
      // Keep templateId for convenience
      Alert.alert('Success', 'Added to queue');
      if (hasShareIntent) {
        resetShareIntent();
      }
    } catch (error) {
      console.error("Error adding document: ", error);
      Alert.alert('Error', 'Failed to add to queue');
    }
    setLoading(false);
  };

  if (!isAuthenticated) {
    return (
      <View style={styles.container}>
        <View style={styles.card}>
          <Text style={styles.title}>Queue Login</Text>
          <TextInput
            style={styles.input}
            placeholder="Password"
            value={password}
            onChangeText={setPassword}
            secureTextEntry
          />
          <TouchableOpacity style={styles.button} onPress={handleLogin}>
            <Text style={styles.buttonText}>Login</Text>
          </TouchableOpacity>
        </View>
      </View>
    );
  }

  return (
    <SafeAreaView style={styles.container}>
      <KeyboardAvoidingView
        behavior={Platform.OS === "ios" ? "padding" : "height"}
        style={styles.content}
      >
        <View style={styles.header}>
          <Text style={styles.headerTitle}>Templatea Queue</Text>
          <TouchableOpacity onPress={handleLogout}>
            <Text style={styles.logoutText}>Logout</Text>
          </TouchableOpacity>
        </View>

        <View style={styles.formCard}>
          <Text style={styles.sectionTitle}>Add to Queue</Text>
          <TextInput
            style={styles.input}
            placeholder="Instagram URL"
            value={url}
            onChangeText={setUrl}
            autoCapitalize="none"
          />
          <TextInput
            style={styles.input}
            placeholder="Template ID (e.g. marketing_spots)"
            value={templateId}
            onChangeText={setTemplateId}
            autoCapitalize="none"
          />
          <TouchableOpacity
            style={[styles.button, loading && styles.buttonDisabled]}
            onPress={handleSubmit}
            disabled={loading}
          >
            {loading ? (
              <ActivityIndicator color="#fff" />
            ) : (
              <Text style={styles.buttonText}>Add to Queue</Text>
            )}
          </TouchableOpacity>
        </View>

        <View style={styles.listContainer}>
          <Text style={styles.sectionTitle}>Recent Queue</Text>
          <FlatList
            data={queue}
            keyExtractor={item => item.id}
            renderItem={({ item }) => (
              <View style={styles.listItem}>
                <View style={styles.row}>
                  <View style={[
                    styles.badge,
                    item.status === 'completed' ? styles.badgeSuccess :
                      item.status === 'processing' ? styles.badgeInfo :
                        item.status === 'failed' ? styles.badgeError :
                          styles.badgeWarning
                  ]}>
                    <Text style={styles.badgeText}>{item.status}</Text>
                  </View>
                  <Text style={styles.timeText}>
                    {item.created_at?.seconds ? new Date(item.created_at.seconds * 1000).toLocaleTimeString() : 'Just now'}
                  </Text>
                </View>
                <Text style={styles.urlText} numberOfLines={1}>{item.url}</Text>
                <View style={styles.row}>
                  <Text style={styles.metaText}>{item.template_id}</Text>
                  {item.workspace_id && <Text style={styles.metaText}>WS: {item.workspace_id}</Text>}
                </View>
                {item.error && <Text style={styles.errorText}>{item.error}</Text>}
                {item.output_link && (
                  <TouchableOpacity
                    style={styles.downloadButton}
                    onPress={() => Linking.openURL(item.output_link)}
                  >
                    <Text style={styles.downloadButtonText}>Download Video</Text>
                  </TouchableOpacity>
                )}
              </View>
            )}
          />
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#f3f4f6',
  },
  content: {
    flex: 1,
    padding: 16,
  },
  card: {
    backgroundColor: '#fff',
    padding: 24,
    borderRadius: 12,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.1,
    shadowRadius: 4,
    elevation: 3,
    width: '100%',
    maxWidth: 400,
    alignSelf: 'center',
    marginTop: 'auto',
    marginBottom: 'auto',
  },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 20,
  },
  headerTitle: {
    fontSize: 24,
    fontWeight: 'bold',
    color: '#1f2937',
  },
  logoutText: {
    color: '#ef4444',
    fontWeight: '600',
  },
  formCard: {
    backgroundColor: '#fff',
    padding: 16,
    borderRadius: 12,
    marginBottom: 20,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.05,
    shadowRadius: 2,
    elevation: 2,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 12,
    color: '#374151',
  },
  input: {
    borderWidth: 1,
    borderColor: '#d1d5db',
    borderRadius: 8,
    padding: 12,
    marginBottom: 12,
    fontSize: 16,
    backgroundColor: '#fff',
  },
  button: {
    backgroundColor: '#2563eb',
    padding: 14,
    borderRadius: 8,
    alignItems: 'center',
  },
  buttonDisabled: {
    backgroundColor: '#93c5fd',
  },
  buttonText: {
    color: '#fff',
    fontWeight: '600',
    fontSize: 16,
  },
  title: {
    fontSize: 24,
    fontWeight: 'bold',
    marginBottom: 20,
    textAlign: 'center',
    color: '#1f2937',
  },
  listContainer: {
    flex: 1,
    backgroundColor: '#fff',
    borderRadius: 12,
    padding: 16,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.05,
    shadowRadius: 2,
    elevation: 2,
  },
  listItem: {
    borderBottomWidth: 1,
    borderBottomColor: '#e5e7eb',
    paddingVertical: 12,
  },
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 4,
  },
  badge: {
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 4,
  },
  badgeSuccess: { backgroundColor: '#dcfce7' },
  badgeInfo: { backgroundColor: '#dbeafe' },
  badgeError: { backgroundColor: '#fee2e2' },
  badgeWarning: { backgroundColor: '#fef9c3' },
  badgeText: {
    fontSize: 10,
    fontWeight: 'bold',
    textTransform: 'uppercase',
    color: '#1f2937',
  },
  timeText: {
    fontSize: 12,
    color: '#9ca3af',
  },
  urlText: {
    fontSize: 14,
    color: '#1f2937',
    marginBottom: 4,
  },
  metaText: {
    fontSize: 12,
    color: '#6b7280',
  },
  errorText: {
    fontSize: 12,
    color: '#ef4444',
    marginTop: 4,
    backgroundColor: '#fef2f2',
    padding: 4,
    borderRadius: 4,
  },
  downloadButton: {
    backgroundColor: '#059669',
    padding: 8,
    borderRadius: 4,
    marginTop: 8,
    alignItems: 'center',
  },
  downloadButtonText: {
    color: '#fff',
    fontSize: 12,
    fontWeight: 'bold',
  },
});
