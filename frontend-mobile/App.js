import React from 'react';
import { NavigationContainer } from '@react-navigation/native';
import { createStackNavigator } from '@react-navigation/stack';
import { Provider as PaperProvider, MD3LightTheme } from 'react-native-paper';
import { SafeAreaProvider } from 'react-native-safe-area-context';

// Screens
import ConnectScreen from './src/screens/ConnectScreen';
import HomeScreen from './src/screens/HomeScreen';
import TemplateScreen from './src/screens/TemplateScreen';
import ProcessingScreen from './src/screens/ProcessingScreen';
import CopySelectionScreen from './src/screens/CopySelectionScreen';
import ResultScreen from './src/screens/ResultScreen';

const Stack = createStackNavigator();

const theme = {
  ...MD3LightTheme,
  colors: {
    ...MD3LightTheme.colors,
    primary: '#6200ee',
    secondary: '#03dac6',
  },
};

export default function App() {
  return (
    <SafeAreaProvider>
      <PaperProvider theme={theme}>
        <NavigationContainer>
          <Stack.Navigator initialRouteName="Connect" screenOptions={{ headerShown: false }}>
            <Stack.Screen name="Connect" component={ConnectScreen} />
            <Stack.Screen name="Home" component={HomeScreen} />
            <Stack.Screen name="Template" component={TemplateScreen} />
            <Stack.Screen name="Processing" component={ProcessingScreen} />
            <Stack.Screen name="CopySelection" component={CopySelectionScreen} />
            <Stack.Screen name="Result" component={ResultScreen} />
          </Stack.Navigator>
        </NavigationContainer>
      </PaperProvider>
    </SafeAreaProvider>
  );
}
